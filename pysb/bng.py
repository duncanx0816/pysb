from __future__ import print_function as _
import pysb.core
from pysb.generator.bng import BngGenerator
import os
import subprocess
import re
import itertools
import sympy
import numpy
import tempfile
from pkg_resources import parse_version
import abc

try:
    from cStringIO import StringIO
except ImportError:
    from io import StringIO
try:
    from future_builtins import zip
except ImportError:
    pass

# Cached value of BNG path
_bng_path = None

def set_bng_path(dir):
    global _bng_path
    _bng_path = os.path.join(dir,'BNG2.pl')
    # Make sure file exists and that it is not a directory
    if not os.access(_bng_path, os.F_OK) or not os.path.isfile(_bng_path):
        raise Exception('Could not find BNG2.pl in ' + os.path.abspath(dir) + '.')
    # Make sure file has executable permissions
    elif not os.access(_bng_path, os.X_OK):
        raise Exception("BNG2.pl in " + os.path.abspath(dir) + " does not have executable permissions.")

def _get_bng_path():
    """
    Return the path to BioNetGen's BNG2.pl.

    Looks for a BNG distribution at the path stored in the BNGPATH environment
    variable if that's set, or else in a few hard-coded standard locations.

    """

    global _bng_path

    # Just return cached value if it's available
    if _bng_path:
        return _bng_path

    path_var = 'BNGPATH'
    dist_dirs = [
        '/usr/local/share/BioNetGen',
        'c:/Program Files/BioNetGen',
        ]
    # BNG 2.1.8 moved BNG2.pl up out of the Perl2 subdirectory, so to be more
    # compatible we check both the old and new locations.
    script_subdirs = ['', 'Perl2']

    def check_dist_dir(dist_dir):
        # Return the full path to BNG2.pl inside a BioNetGen distribution
        # directory, or False if directory does not contain a BNG2.pl in one of
        # the expected places.
        for subdir in script_subdirs:
            script_path = os.path.join(dist_dir, subdir, 'BNG2.pl')
            if os.access(script_path, os.F_OK):
                return script_path
        else:
            return False

    # First check the environment variable, which has the highest precedence
    if path_var in os.environ:
        script_path = check_dist_dir(os.environ[path_var])
        if not script_path:
            raise Exception('Environment variable %s is set but BNG2.pl could'
                            ' not be found there' % path_var)
    # If the environment variable isn't set, check the standard locations
    else:
        for dist_dir in dist_dirs:
            script_path = check_dist_dir(dist_dir)
            if script_path:
                break
        else:
            raise Exception('Could not find BioNetGen installed in one of the '
                            'following locations:' +
                            ''.join('\n    ' + d for d in dist_dirs))
    # Cache path for future use
    _bng_path = script_path
    return script_path

class GenerateNetworkError(RuntimeError):
    """BNG reported an error when trying to generate a network for a model."""
    pass


class BngBaseInterface(object):
    """ Abstract base class for interfacing with BNG """
    __metaclass__ = abc.ABCMeta

    def __init__(self, model, verbose=False, cleanup=False,
                 output_prefix=None):
        self.verbose = verbose
        self.cleanup = cleanup
        self.output_prefix = 'tmpBNG' if output_prefix is None else \
            output_prefix
        self.generator = BngGenerator(model)
        self._check_model()

    def __enter__(self):
        return self

    @abc.abstractmethod
    def __exit__(self):
        return

    def _check_model(self):
        """
        Checks a model has at least one initial condition and rule, raising
        an exception if not
        """
        if not self.model.initial_conditions:
            raise NoInitialConditionsError()
        if not self.model.rules:
            raise NoRulesError()

    @classmethod
    def _bng_param(cls, param):
        """
        Ensures a BNG console parameter is in the correct format

        Strings are double quoted and booleans are mapped to [0,1]. Other
        types are currently used verbatim.

        Parameters
        ----------
        param :
            An argument to a BNG action call
        """
        if isinstance(param, str):
            return '"%s"' % param
        if isinstance(param, bool):
            return 1 if param else 0
        return param

    @abc.abstractmethod
    def action(self, action, **kwargs):
        """
        Generates code to execute a BNG action command

        Parameters
        ----------
        action: string
            The name of the BNG action function
        kwargs: kwargs, optional
            Arguments and values to supply to BNG
        """
        if kwargs:
            action_args = ','.join('%s=>%s' % (k, BngConsole._bng_param(v))
                                   for k, v in kwargs.items())
        else:
            action_args = ''
        return action_args

    @property
    def model(self):
        """
        Convenience method to get the PySB model itself

        :return: PySB model used in BNG interface constructor
        """
        return self.generator.model

    @property
    def base_filename(self):
        """
        Returns the base filename (without extension) for BNG output files
        """
        return os.path.splitext(self.bng_filename)[0]


class BngConsole(BngBaseInterface):
    """ Interact with BioNetGen through BNG Console """
    def __init__(self, model, verbose=False, cleanup=True,
                 output_dir=None, output_prefix=None, timeout=30):
        super(BngConsole, self).__init__(model, verbose, cleanup,
                                         output_prefix)

        try:
            import pexpect
        except ImportError:
            raise ImportError("Library 'pexpect' is required to use "
                              "BNGConsole, please install it to continue.\n"
                              "It is not currently available on Windows.")

        try:
            # Generate BNGL file
            with tempfile.NamedTemporaryFile(mode='w', suffix='.bngl',
                                             prefix=self.output_prefix,
                                             dir=output_dir,
                                             delete=False) as bng_file:
                self.bng_filename = bng_file.name
                bng_file.write(self.generator.get_content())

            # Start BNG Console and load BNGL
            self.console = pexpect.spawn('perl %s --console' % _get_bng_path(),
                                         cwd=os.path.dirname(
                                             self.bng_filename),
                                         timeout=timeout)
            self._console_wait()
            self.console.sendline('load %s' % bng_file.name)
            self._console_wait()
        finally:
            if self.bng_filename:
                try:
                    os.unlink(self.bng_filename)
                except OSError:
                    pass

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        In console mode, commands have already been executed, so we simply
        close down the console when down.
        """
        self.console.sendline('done')
        self.console.close()

    def _console_wait(self):
        """
        Wait for BNG console to process the command, and return the output
        :return: BNG console output from the previous command
        """
        self.console.expect('BNG>')
        if self.verbose:
            print(self.console.before)
        return self.console.before

    def generate_network(self, overwrite=False):
        """
        Generates a network in BNG and returns the network file contents as
        a string

        Parameters
        ----------
        overwrite: bool, optional
            Overwrite existing network file, if any
        """
        try:
            net_filename = self.base_filename + '.net'
            self.action('generate_network', overwrite=overwrite)
            with open(net_filename, 'r') as net_file:
                bng_network = net_file.read()
        finally:
            if self.cleanup:
                try:
                    os.unlink(net_filename)
                except OSError:
                    # File may have been deleted already, or be open elsewhere
                    pass
        return bng_network

    def action(self, action, **kwargs):
        """
        Generates a BNG action command and executes it through the console,
        returning any console output

        Parameters
        ----------
        action : string
            The name of the BNG action function
        kwargs : kwargs, optional
            Arguments and values to supply to BNG
        """
        # Process BNG arguments into a string using supermethod
        action_args = super(BngConsole, self).action(action, **kwargs)

        # Execute the command via the console
        self.console.sendline('action %s({%s})' % (action, action_args))

        # Wait for the command to execute and return the result
        return self._console_wait()


class BngFileInterface(BngBaseInterface):
    def __init__(self, model, verbose=False, output_dir=None,
                 output_prefix=None, cleanup=True):
        super(BngFileInterface, self).__init__(model, verbose, cleanup,
                                               output_prefix)
        self.command_queue = StringIO()
        self.command_queue.write('begin actions\n')

        # Create the BNGL file now so we know where to find it
        with tempfile.NamedTemporaryFile(mode='w', suffix='.bngl',
                                         prefix=self.output_prefix,
                                         dir=output_dir,
                                         delete=False) as bng_file:
            self.bng_filename = bng_file.name

    def __exit__(self, exc_type, exc_val, exc_tb):
        """
        In file mode, we execute all the queued commands when leaving the
        context manager (i.e. now) by adding them to the BNGL
        """
        self.command_queue.write('end actions\ndone\n')
        try:
            # Generate BNGL file
            with open(self.bng_filename, 'w') as bng_file:
                bng_file.write(self.generator.get_content())
                bng_file.write(self.command_queue.getvalue())

            p = subprocess.Popen(['perl', _get_bng_path(), self.bng_filename],
                                 cwd=os.path.dirname(self.bng_filename),
                                 stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE)
            if self.verbose:
                for line in iter(p.stdout.readline, b''):
                    print(line, end="")
            (p_out, p_err) = p.communicate()
            if p.returncode:
                raise GenerateNetworkError(p_out.rstrip("at line") +
                                           "\n" +
                                           p_err.rstrip())
        finally:
            try:
                os.unlink(self.bng_filename)
            except OSError:
                pass

    def action(self, action, **kwargs):
        """
        Generates a BNG action command and adds it to the command queue

        Parameters
        ----------
        action : string
            The name of the BNG action function
        kwargs : kwargs, optional
            Arguments and values to supply to BNG
        """
        # Process BNG arguments into a string using supermethod
        action_args = super(BngFileInterface, self).action(action, **kwargs)

        # Add the command to the queue
        self.command_queue.write('\t%s({%s})\n' % (action, action_args))

        return


def run_ssa(model, t_end=10, n_steps=100, param_values=None, output_dir=None,
            output_file_basename=None, cleanup=True, verbose=False,
            **additional_args):
    """
    Simulate a model with BNG's SSA simulator and return the trajectories.

    Parameters
    ----------
    model : Model
        Model to simulate.
    t_end : number, optional
        Final time point of the simulation.
    n_steps : int, optional
        Number of steps in the simulation.
    param_values : vector-like or dictionary, optional
            Values to use for every parameter in the model. Ordering is
            determined by the order of model.parameters. 
            If not specified, parameter values will be taken directly from
            model.parameters.
    output_dir : string, optional
        Location for temporary files generated by BNG. If None (the
        default), uses a temporary directory provided by the system.
    output_file_basename : string, optional
        The basename for the .bngl, .gdat, .cdat, and .net files that are
        generated by BNG. If None (the default), creates a random basename.
    cleanup : bool, optional
        If True (default), delete the temporary files after the simulation is
        finished. If False, leave them in place (in `output_dir`). Useful for
        debugging.
    verbose: bool, optional
        If True, print BNG screen output.
    additional_args: kwargs, optional
        Additional arguments to pass to BioNetGen

    """
    additional_args['method'] = 'ssa'
    additional_args['t_end'] = t_end
    additional_args['n_steps'] = n_steps
    additional_args['verbose'] = verbose

    if param_values is not None:
        if len(param_values) != len(model.parameters):
            raise Exception("param_values must be the same length as model.parameters")
        for i in range(len(param_values)):
            model.parameters[i].value = param_values[i]

    try:
        with BngFileInterface(model, verbose=verbose, output_dir=output_dir,
                              output_prefix=output_file_basename,
                              cleanup=cleanup) as con:
            output_file_basename = con.base_filename
            gdat_filename = output_file_basename + '.gdat'
            cdat_filename = output_file_basename + '.cdat'
            net_filename = output_file_basename + '.net'

            con.action('generate_network', overwrite=True, verbose=verbose)
            con.action('simulate', **additional_args)

        # Read in the network file
        with open(net_filename, 'r') as net_file:
            output = net_file.read()

        # Parse netfile (in case this hasn't already been done)
        if not model.species:
            _parse_netfile(model, iter(output.split('\n')))

        cdat_arr = numpy.loadtxt(cdat_filename, skiprows=1) # keep first column (time)
        if len(model.observables):
            gdat_arr = numpy.loadtxt(gdat_filename, skiprows=1)[:,1:] # exclude first column (time)
        else:
            gdat_arr = numpy.ndarray((len(cdat_arr), 0))

        names = ['time'] + ['__s%d' % i for i in range(cdat_arr.shape[1]-1)] # -1 for time column
        yfull_dtype = list(zip(names, itertools.repeat(float)))
        if len(model.observables):
            yfull_dtype += list(zip(model.observables.keys(),
                                    itertools.repeat(float)))
        yfull = numpy.ndarray(len(cdat_arr), yfull_dtype)
        
        yfull_view = yfull.view(float).reshape(len(yfull), -1)
        yfull_view[:, :len(names)] = cdat_arr 
        yfull_view[:, len(names):] = gdat_arr

    finally:
        if cleanup:
            for filename in [gdat_filename,
                             cdat_filename, net_filename]:
                try:
                    os.unlink(filename)
                except OSError:
                    pass

    return yfull


def generate_network(model, cleanup=True, append_stdout=False, verbose=False):
    """
    Return the output from BNG's generate_network function given a model.

    The output is a BNGL model definition with additional sections 'reactions'
    and 'groups', and the 'species' section expanded to contain all possible
    species. BNG refers to this as a 'net' file.

    Parameters
    ----------
    model : Model
        Model to pass to generate_network.
    cleanup : bool, optional
        If True (default), delete the temporary files after the simulation is
        finished. If False, leave them in place (in `output_dir`). Useful for
        debugging.
    append_stdout : bool, optional
        This option is no longer supported and has been left here for API
        compatibility reasons.
    verbose : bool, optional
        If True, print output from BNG to stdout.
    """
    with BngFileInterface(model, verbose=verbose, cleanup=cleanup) as con:
        net_filename = con.base_filename + '.net'
        con.action('generate_network', overwrite=True, verbose=verbose)

    with open(net_filename, 'r') as net_file:
            output = net_file.read()

    return output


def generate_equations(model, cleanup=True, verbose=False):
    """
    Generate math expressions for reaction rates and species in a model.

    This fills in the following pieces of the model:

    * odes
    * species
    * reactions
    * reactions_bidirectional
    * observables (just `coefficients` and `species` fields for each element)

    """
    # only need to do this once
    # TODO track "dirty" state, i.e. "has model been modified?"
    #   or, use a separate "math model" object to contain ODEs
    if model.odes:
        return
    lines = iter(generate_network(model,cleanup,verbose=verbose).split('\n'))
    _parse_netfile(model, lines)


def _parse_netfile(model, lines):
    """Parse 'species', 'reactions', and 'groups' blocks from a BNGL net file."""
    try:
        global new_reverse_convention
        (bng_version, bng_codename) = re.match(r'# Created by BioNetGen (\d+\.\d+\.\d+)(?:-(\w+))?$', next(lines)).groups()
        if parse_version(bng_version) > parse_version("2.2.6") or parse_version(bng_version) == parse_version("2.2.6") and bng_codename == "stable":
            new_reverse_convention = True
        else:
            new_reverse_convention = False

        while 'begin species' not in next(lines):
            pass
        model.species = []
        while True:
            line = next(lines)
            if 'end species' in line: break
            _parse_species(model, line)

        while 'begin reactions' not in next(lines):
            pass
        model.odes = [sympy.numbers.Zero()] * len(model.species)
        global reaction_cache
        reaction_cache = {}
        while True:
            line = next(lines)
            if 'end reactions' in line: break
            _parse_reaction(model, line)
        # fix up reactions whose reverse version we saw first
        for r in model.reactions_bidirectional:
            if r['reverse']:
                r['reactants'], r['products'] = r['products'], r['reactants']
                r['rate'] *= -1
            # now the 'reverse' value is no longer needed
            del r['reverse']

        while 'begin groups' not in next(lines):
            pass
        while True:
            line = next(lines)
            if 'end groups' in line: break
            _parse_group(model, line)

    except StopIteration as e:
        pass


def _parse_species(model, line):
    """Parse a 'species' line from a BNGL net file."""
    index, species, value = line.strip().split()
    species_compartment_name, complex_string = re.match(r'(?:@(\w+)::)?(.*)', species).groups()
    species_compartment = model.compartments.get(species_compartment_name)
    monomer_strings = complex_string.split('.')
    monomer_patterns = []
    for ms in monomer_strings:
        monomer_name, site_strings, monomer_compartment_name = re.match(r'(\w+)\(([^)]*)\)(?:@(\w+))?', ms).groups()
        site_conditions = {}
        if len(site_strings):
            for ss in site_strings.split(','):
                # FIXME this should probably be done with regular expressions
                if '!' in ss and '~' in ss:
                    site_name, condition = ss.split('~')
                    state, bond = condition.split('!')
                    if bond == '?':
                        bond = pysb.core.WILD
                    elif bond == '!':
                        bond = pysb.core.ANY
                    else:
                        bond = int(bond)
                    condition = (state, bond)
                elif '!' in ss:
                    site_name, condition = ss.split('!', 1)
                    if '!' in condition:
                        condition = [int(c) for c in condition.split('!')]
                    else:
                        condition = int(condition)
                elif '~' in ss:
                    site_name, condition = ss.split('~')
                else:
                    site_name, condition = ss, None
                site_conditions[site_name] = condition
        monomer = model.monomers[monomer_name]
        monomer_compartment = model.compartments.get(monomer_compartment_name)
        # Compartment prefix notation in BNGL means "assign this compartment to
        # all molecules without their own explicit compartment".
        compartment = monomer_compartment or species_compartment
        mp = pysb.core.MonomerPattern(monomer, site_conditions, compartment)
        monomer_patterns.append(mp)

    cp = pysb.core.ComplexPattern(monomer_patterns, None)
    model.species.append(cp)


def _parse_reaction(model, line):
    """Parse a 'reaction' line from a BNGL net file."""
    (number, reactants, products, rate, rule) = line.strip().split(' ', 4)
    # the -1 is to switch from one-based to zero-based indexing
    reactants = tuple(int(r) - 1 for r in reactants.split(','))
    products = tuple(int(p) - 1 for p in products.split(','))
    rate = rate.rsplit('*')
    (rule_list, unit_conversion) = re.match(
                    r'#([\w,\(\)]+)(?: unit_conversion=(.*))?\s*$', rule).groups()
    rule_list = rule_list.split(',') # BNG lists all rules that generate a rxn
    # Support new (BNG 2.2.6-stable or greater) and old BNG naming convention for reverse rules
    rule_name, is_reverse = zip(*[re.subn('^_reverse_|\(reverse\)$', '', r) for r in rule_list])
    is_reverse = tuple(bool(i) for i in is_reverse)
    r_names = ['__s%d' % r for r in reactants]
    combined_rate = sympy.Mul(*[sympy.Symbol(t) for t in r_names + rate])
    reaction = {
        'reactants': reactants,
        'products': products,
        'rate': combined_rate,
        'rule': rule_name,
        'reverse': is_reverse,
        }
    model.reactions.append(reaction)
    # bidirectional reactions
    key = (reactants, products)
    key_reverse = (products, reactants)
    reaction_bd = reaction_cache.get(key_reverse)
    if reaction_bd is None:
        # make a copy of the reaction dict
        reaction_bd = dict(reaction)
        # default to false until we find a matching reverse reaction
        reaction_bd['reversible'] = False
        reaction_cache[key] = reaction_bd
        model.reactions_bidirectional.append(reaction_bd)
    else:
        reaction_bd['reversible'] = True
        reaction_bd['rate'] -= combined_rate
        reaction_bd['rule'] += tuple(r for r in rule_name if r not in reaction_bd['rule'])
    # odes
    for p in products:
        model.odes[p] += combined_rate
    for r in reactants:
        model.odes[r] -= combined_rate
            
            
def _parse_group(model, line):
    """Parse a 'group' line from a BNGL net file."""
    # values are number (which we ignore), name, and species list
    values = line.strip().split()
    obs = model.observables[values[1]]
    if len(values) == 3:
        # combination is a comma separated list of [coeff*]speciesnumber
        for product in values[2].split(','):
            terms = product.split('*')
            # if no coeff given (just species), insert a coeff of 1
            if len(terms) == 1:
                terms.insert(0, 1)
            obs.coefficients.append(int(terms[0]))
            # -1 to change to 0-based indexing
            obs.species.append(int(terms[1]) - 1)


class NoInitialConditionsError(RuntimeError):
    """Model initial_conditions is empty."""
    def __init__(self):
        RuntimeError.__init__(self, "Model has no initial conditions")

class NoRulesError(RuntimeError):
    """Model rules is empty."""
    def __init__(self):
        RuntimeError.__init__(self, "Model has no rules")
