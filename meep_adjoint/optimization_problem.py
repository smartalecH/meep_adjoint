"""OptimizationProblem is the top-level class exported by the meep.adjoint module.
"""
import os
import inspect

import meep as mp

from . import (DFTCell, ObjectiveFunction, TimeStepper, FiniteElementBasis, rescale_sources, E_CPTS)


######################################################################
######################################################################
######################################################################
class OptimizationProblem(object):
    """Top-level class in the MEEP adjoint module.

    Intended to be instantiated from user scripts with mandatory constructor
    input arguments specifying the data required to define an adjoint-based
    optimization.

    The class knows how to do one basic thing: Given an input vector
    of design variables, compute the objective function value (forward
    calculation) and optionally its gradient (adjoint calculation).
    This is done by the __call__ method. The actual computations
    are delegated to a hierarchy of lower-level classes, of which
    the uppermost is TimeStepper.

    """

    def __init__(self, 
                 sim,
                 objective_regions,
                 basis,
                 objective_function,
                 beta_vector,
                 design_function
                 ):
        """
        Parameters:
        -----------

        cell_size: array-like
        background_geometry: list of meep.GeometricObject
        foreground_geometry: list of meep.GeometricObject

              Size of computational cell and lists of GeometricObjects
              that {precede,follow} the design object in the overall geometry.

        sources: list of meep.Source
        source_region: Subregion
            (*either* `sources` **or** `source_region` should be non-None)
            Specification of forward source distribution, i.e. the source excitation(s) that
            produce the fields from which the objective function is computed.

            In general, sources will be an arbitrary caller-created list of Source
            objects, in which case source_region, source_component are ignored.

            As an alternative convenience convention, the caller may omit
            sources and instead specify source_region; in this case, a
            source distribution over the given region is automatically
            created based on the values of the module-wide configuration
            options fcen, df, source_component, source_mode.

        objective regions: list of Subregion
             subregions of the computational cell over which frequency-domain
             fields are tabulated and used to compute objective quantities

        basis: (Basis)
        design_region: (Subregion)
              (*either* `basis` **or** `design_region` should be non-None)
              Specification of function space for the design permittivity.

              In general, basis will be a caller-created instance of
              some subclass of meep.adjoint.Basis. Then the spatial
              extent of the design region is determined by basis and
              the design_region argument is ignored.

              As an alternative convenience convention, the caller
              may omit basis and set design_region; in this case,
              an appropriate basis for the given design_region is
              automatically created based on the values of various
              module-wide adj_opt such as 'element_type' and
              'element_length'. This convenience convention is
              only available for box-shaped (hyperrectangular)
              design regions.

        extra_regions: list of Subregion
            Optional list of additional subregions over which to tabulate frequency-domain
            fields.

        objective_function: str
            definition of the quantity to be maximized. This should be
            a mathematical expression in which the names of one or more
            objective quantities appear, and which should evaluate to
            a real number when numerical values are substituted for the
            names of all objective quantities.

        extra_quantities: list of str
            Optional list of additional objective quantities to be computed and reported
            together with the objective function.
        """

        #-----------------------------------------------------------------------
        # process convenience arguments:
        #  (a) if no basis was specified, create one using the given design
        #      region plus global option values
        #  (b) if no sources were specified, create one using the given source
        #      region plus global option values
        #-----------------------------------------------------------------------
        self.basis = basis
        design_region = self.basis.domain

        sources = sim.sources
        rescale_sources(sources)

        #-----------------------------------------------------------------------
        # initialize lower-level helper classes
        #-----------------------------------------------------------------------
        # DFTCells
        dft_cell_names  = []
        objective_cells = [ DFTCell(r) for r in objective_regions]
        design_cell     = DFTCell(design_region, E_CPTS)
        dft_cells       = objective_cells + [design_cell]

        # ObjectiveFunction
        obj_func        = ObjectiveFunction(fstr=objective_function)

        # initial values of (a) design variables, (b) the spatially-varying
        # permittivity function they define (the 'design function'), (c) the
        # GeometricObject describing a material body with this permittivity
        # (the 'design object'), and (d) mp.Simulation superposing the design
        # object with the rest of the caller's geometry.
        # Note that sources and DFT cells are not added to the Simulation at
        # this stage; this is done later by internal methods of TimeStepper
        # on a just-in-time basis before starting a timestepping run.

        self.design_function = design_function

        # TimeStepper
        self.stepper    = TimeStepper(obj_func, dft_cells, self.basis, sim, sources)

    #####################################################################
    # The basic task of an OptimizationProblem: Given a candidate design
    # function, compute the objective function value and (optionally) gradient.
    ######################################################################
    def __call__(self, beta_vector=None, design=None,
                       need_value=True, need_gradient=True):
        """Evaluate value and/or gradient of objective function.

        Parameters
        ----------
        beta_vector: np.array
                new vector of design variables

        design: function-like
                alternative to beta_vector: function that will be projected
                onto the basis to obtain the new vector of design variables

        need_value: boolean
                if False, the forward run to compute the objective-function
                value will be omitted. This is only useful if the forward run
                has already been done (for the current design function) e.g.
                by a previous call with need_gradient = False.

        need_gradient: boolean
                if False, the adjoint run to compute the objective-function
                gradient will be omitted.


        Returns
        -------
        2-tuple (fq, gradf), where

            fq = np.array([f q1 q2 ... qN])
               = values of objective function & objective quantities

            gradf = np.array([df/dbeta_1 ... df/dbeta_D]), i.e. vector of partial
                    f derivatives w.r.t. each design variable (if need_gradient==True)

            If need_value or need_gradient is False, then fq or gradf in the return
            tuple will be None.
        """
        if (beta_vector is not None) or (design is not None):
            self.update_design(beta_vector=beta_vector, design=design)

        #######################################################################
        # sanity check: if they are asking for an adjoint calculation with no
        #               forward calculation, make sure we previously completed
        #               a forward calculation for the current design function
        #######################################################################
        if need_value == False and self.stepper.state == 'reset':
            warnings.warn('forward run not yet run for this design; ignoring request to omit')
            need_value = True

        fq    = self.stepper.run('forward') if need_value else None
        gradf = self.stepper.run('adjoint') if need_gradient else None

        return fq, gradf


    def get_fdf_funcs(self):
        """construct callable functions for objective function value and gradient

        Returns
        -------
        2-tuple (f_func, df_func) of standalone (non-class-method) callables, where
            f_func(beta) = objective function value for design variables beta
           df_func(beta) = objective function gradient for design variables beta
        """

        def _f(x=None):
            (fq, _) = self.__call__(beta_vector = x, need_gradient = False)
            return fq[0]

        def _df(x=None):
            (_, df) = self.__call__(need_value = False)
            return df

        return _f, _df


    #####################################################################
    # ancillary API methods #############################################
    #####################################################################
    def update_design(self, beta_vector=None, design=None):
        """Update the design permittivity function.

           Precisely one of (beta_vector, design) should be specified.

           If beta_vector is specified, it simply replaces the old
           beta_vector wholesale.

           If design is specified, the function it describes is projected
           onto the basis set to yield the new beta_vector.

        Parameters
        ----------
        beta_vector: np.array
            basis expansion coefficients for new permittivity function

        design: function-like
            new permittivity function
        """
        self.beta_vector = self.basis.project(design) if design else beta_vector
        self.design_function.set_coefficients(self.beta_vector)
        self.stepper.state='reset'


    #####################################################################
    #####################################################################
    #####################################################################
    def visualize(self, id=None, pmesh=False):
        """Produce a graphical visualization of the geometry and/or fields,
           as appropriately autodetermined based on the current state of
           progress.
        """
        
        if self.stepper.state=='reset':
            self.stepper.prepare('forward')

        bs = self.basis
        mesh = bs.fs.mesh() if (hasattr(bs,'fs') and hasattr(bs.fs,'mesh')) else None

        fig = plt.figure(num=id) if id else None

        self.stepper.sim.plot2D()
        if mesh is not None and pmesh:
            plot_mesh(mesh)
        '''
        if self.stepper.state.endswith('.prepared'):
            visualize_sim(self.stepper.sim, self.stepper.dft_cells, mesh=mesh, fig=fig, options=options)
        elif self.stepper.state == 'forward.complete':
            visualize_sim(self.stepper.sim, self.stepper.dft_cells, mesh=mesh, fig=fig, options=options)
        #else self.stepper.state == 'adjoint.complete':
        #            visualize_sim(self.stepper.sim, self.stepper.dft_cells, mesh=mesh, fig=fig, options=options)
        '''

def plot_mesh(mesh,lc='g',lw=1):
    """Helper function. Invoke FENICS/dolfin plotting routine to plot FEM mesh"""

    keys = ['linecolor', 'linewidth']
    if lw==0.0:
        return
    try:
        import dolfin as df
        df.plot(mesh, color=lc, linewidth=lw)
    except ImportError:
        warnings.warn('failed to import dolfin module; omitting FEM mesh plot')