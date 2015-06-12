import networkx
import sympy
import re
import copy
import numpy
import sympy.parsing.sympy_parser
import itertools
import matplotlib.pyplot as plt
import pysb
from pysb.integrate import odesolve

# TODO: Change 'masters' to 'drivers' and 'slaves' to 'passengers'.

def Heaviside_num(x):
    return 0.5*(numpy.sign(x)+1)

class Tropical:
    def __init__(self, model):
        self.model            = model
        self.y                = None  # ode solution, numpy array
        self.slaves           = None
        self.graph            = None
        self.cycles           = []
        self.conservation     = None
        self.conserve_var     = None
        self.value_conservation = {}
        self.full_names       = {}

    def __repr__(self):
        return "<%s '%s' (slaves: %s, cycles: %d) at 0x%x>" % \
            (self.__class__.__name__, self.model.name,
             self.slaves.__repr__(),
             len(self.cycles),
             id(self))

    def tropicalize(self,t, ignore=1, epsilon=2, rho=3, verbose=True):
        if verbose: print "Solving Simulation"
        self.y = odesolve(self.model, t)
        
        # Only concrete species are considered, and the names must be made to match
        names           = [n for n in filter(lambda n: n.startswith('__'), self.y.dtype.names)]
        self.y          = self.y[names]
        names           = [n.replace('__','') for n in names]
        self.y.dtype    = [(n,'<f8') for n in names]    
          
        if verbose: print "Getting slaved species"
        self.find_slaves(self.y[ignore:], verbose, epsilon)
        if verbose: print "Constructing Graph"
        self.construct_graph()
        if verbose: print "Computing Cycles"
        self.cycles = list(networkx.simple_cycles(self.graph))
        if verbose: print "Computing Conservation laws"
        (self.conservation, self.conserve_var, self.value_conservation) = self.mass_conserved(self.y[ignore:])
        if verbose: print "Pruning Equations"
        self.pruned = self.pruned_equations(self.y[ignore:], rho)
        if verbose: print "Solving pruned equations"
        self.sol_pruned = self.solve_pruned()
        if verbose: print "equation to tropicalize"
        self.eqs_for_tropicalization = self.equations_to_tropicalize()
        if verbose: print "Getting tropicalized equations"
        self.tropical_eqs = self.final_tropicalization()
        self.range_dominating_monomials(self.y[ignore:])
        
        return 

    def find_slaves(self, y, verbose=False, epsilon=None):
        self.slaves = []
        a = []               # list of solved polynomial equations
        b = []               # b is the list of differential equations   

        # Loop through all equations (i is equation number)
        for i, eq in enumerate(self.model.odes):
            eq        = eq.subs('s%d' % i, 's%dstar' % i)
            sol       = sympy.solve(eq, sympy.Symbol('s%dstar' % i))        # Find equation of imposed trace
            for j in range(len(sol)):        # j is solution j for equation i (mostly likely never greater than 2)
                for p in self.model.parameters: sol[j] = sol[j].subs(p.name, p.value)    # Substitute parameters
                a.append(sol[j])
                b.append(i)
        for k,e in enumerate(a):    # a is the list of solution of polinomial equations, b is the list of differential equations
            args = []               #arguments to put in the lambdify function
            variables = [atom for atom in a[k].atoms(sympy.Symbol) if not re.match(r'\d',str(atom))]
            f = sympy.lambdify(variables, a[k], modules = dict(sqrt=numpy.lib.scimath.sqrt) )
            for u,l in enumerate(variables):
                args.append(y[:][str(l)])
            hey = abs(f(*args) - y[:]['s%d'%b[k]])
            if hey.max() <= epsilon : self.slaves.append(b[k])            
                
        return self.slaves


    # This is a function which builds the edges according to the nodes
    def r_link(self, graph, s, r, **attrs):
        nodes = (s, r)
        if attrs.get('_flip'):
            del attrs['_flip']
            nodes = reversed(nodes)
        attrs.setdefault('arrowhead', 'normal')
        graph.add_edge(*nodes, **attrs)

    def construct_graph(self):
        if(self.model.odes == None or self.model.odes == []):
            pysb.bng.generate_equations(model)

        self.graph = networkx.DiGraph(rankdir="LR")
        ic_species = [cp for cp, parameter in self.model.initial_conditions]
        for i, cp in enumerate(self.model.species):
            species_node = i
            self.graph.add_node(species_node, label=species_node)
        for i, reaction in enumerate(self.model.reactions):       
            reactants = set(reaction['reactants'])
            products = set(reaction['products']) 
            attr_reversible = {}
            for s in reactants:
                for p in products:
                    self.r_link(self.graph, s, p, **attr_reversible)
        return self.graph

    #This function finds conservation laws from the conserved cycles
    def mass_conserved(self, y, verbose=False):
        if(self.model.odes == None or self.model.odes == []):
            pysb.bng.generate_equations(self.model)
        h = [] # Array to hold conservation equation
        g = [] # Array to hold corresponding lists of free variables in conservation equations
        value_constants = {} #Dictionary that storage the value of each constant
        for i, item in enumerate(self.cycles):
            b = 0
            u = 0
            for j, specie in enumerate(item):
                b += self.model.odes[self.cycles[i][j]]
            if b == 0:
                g.append(item)
                for l,k in enumerate(item):
                    u += sympy.Symbol('s%d' % self.cycles[i][l])    
                h.append(u-sympy.Symbol('C%d'%i))
                if verbose: print '  cycle%d'%i, 'is conserved'
        
        for i in h:
            constant_to_solve = [atom for atom in i.atoms(sympy.Symbol) if re.match(r'[C]',str(atom))]
            solution = sympy.solve(i, constant_to_solve ,implicit = True)
            solution_ready = solution[0]
            for q in solution_ready.atoms(sympy.Symbol): solution_ready = solution_ready.subs(q, y[0][str(q)])
            value_constants[constant_to_solve[0]] = solution_ready
            
        (self.conservation, self.conserve_var, self.value_conservation) = h, g, value_constants     
        return h, g, value_constants

    def slave_equations(self):
        if(self.model.odes == None or self.model.odes == []):
            eq = self.model.odes
        slave_conserved_eqs = {}
        for i, j in enumerate(self.slaves):
            slave_conserved_eqs[j] = self.model.odes[self.slaves[i]]
        return slave_conserved_eqs

    def find_nearest_zero(self, array):
        idx = (numpy.abs(array)).argmin()
        return array[idx]

    # Make sure this is the "ignore:" y
    def pruned_equations(self, y, rho=1):
        pruned_eqs = self.slave_equations()
        eqs        = copy.deepcopy(pruned_eqs)

        for i, j in enumerate(eqs):
            ble = eqs[j].as_coefficients_dict().keys() # Get monomials
            for l, m in enumerate(ble): #Compares the monomials to find the pruned system
                m_ready = m # Monomial to compute with
                m_elim  = m # Monomial to save
                for p in self.model.parameters: m_ready = m_ready.subs(p.name, p.value) # Substitute parameters
                for k in range(len(ble)):
                    if (k+l+1) <= (len(ble)-1):
                        ble_ready = ble[k+l+1] # Monomial to compute with
                        ble_elim  = ble[k+l+1] # Monomial to save
                        for p in self.model.parameters: ble_ready = ble_ready.subs(p.name, p.value) # Substitute parameters
                        args2 = []
                        args1 = []
                        variables_ble_ready = [atom for atom in ble_ready.atoms(sympy.Symbol) if not re.match(r'\d',str(atom))]
                        variables_m_ready = [atom for atom in m_ready.atoms(sympy.Symbol) if not re.match(r'\d',str(atom))]
                        f_ble = sympy.lambdify(variables_ble_ready, ble_ready, 'numpy' )
                        f_m = sympy.lambdify(variables_m_ready, m_ready, 'numpy' )
                        for uu,ll in enumerate(variables_ble_ready):
                            args2.append(y[:][str(ll)])
                        for w,s in enumerate(variables_m_ready):
                            args1.append(y[:][str(s)])
                        hey_pruned = f_m(*args1) - f_ble(*args2)
                        diff = self.find_nearest_zero(hey_pruned)
                        diff_pru = numpy.abs(diff)
                        if diff > 0 and diff_pru > rho:
                            pruned_eqs[j] = pruned_eqs[j].subs(ble_elim, 0)
                        if diff < 0 and diff_pru > rho:\
                            pruned_eqs[j] = pruned_eqs[j].subs(m_elim, 0)   
                            
        for i, l in enumerate(self.conservation): #Add the conservation laws to the pruned system
            pruned_eqs['cons%d'%i]=l
        self.pruned = pruned_eqs
        return pruned_eqs

    def solve_pruned(self):
        solve_for = copy.deepcopy(self.slaves)
        eqs       = copy.deepcopy(self.pruned)
        eqs_l = []
        for i in eqs.keys():
            eqs_l.append(eqs[i])
            
        
        for i in self.conserve_var:
            if len(i) == 1:
                solve_for.append(i[0])
        variables =  [sympy.Symbol('s%d' %var) for var in solve_for ]
        sol = sympy.solve(eqs_l, variables)

        if len(sol) == 0:
            self.sol_pruned = { j:sympy.Symbol('s%d'%j) for i, j in enumerate(solve_for) }
        else:
            self.sol_pruned = { j:sol[0][i] for i, j in enumerate(solve_for) }
       
        return self.sol_pruned

    def equations_to_tropicalize(self):
        idx = list(set(range(len(self.model.odes))) - set(self.sol_pruned.keys()))
        eqs = { i:self.model.odes[i] for i in idx }

        for l in eqs.keys(): #Substitutes the values of the algebraic system
#             for k in self.sol_pruned.keys(): eqs[l]=eqs[l].subs(sympy.Symbol('s%d' % k), self.sol_pruned[k])
            for q in self.value_conservation.keys(): eqs[l] = eqs[l].subs(q, self.value_conservation[q])
#         for i in eqs.keys():
#             for par in self.model.parameters: eqs[i] = sympy.simplify(eqs[i].subs(par.name, par.value))
        self.eqs_for_tropicalization = eqs

        return eqs
    
    def final_tropicalization(self):
        tropicalized = {}
        
        for j in sorted(self.eqs_for_tropicalization.keys()):
            if type(self.eqs_for_tropicalization[j]) == sympy.Mul: print  sympy.solve(sympy.log(j), dict = True) #If Mul=True there is only one monomial
            elif self.eqs_for_tropicalization[j] == 0: print 'there are no monomials'
            else:            
                ar = self.eqs_for_tropicalization[j].args #List of the terms of each equation  
                asd=0 
                for l, k in enumerate(ar):
                    p = k
                    for f, h in enumerate(ar):
                       if k != h:
                          p *= sympy.Heaviside(sympy.log(abs(k)) - sympy.log(abs(h)))
                    asd +=p
                tropicalized[j] = asd

        self.tropical_eqs = tropicalized
        return tropicalized


    def range_dominating_monomials(self, y): 
        tropical_system = self.final_tropicalization()
        colors = itertools.cycle(["b", "g", "c", "m", "y", "k" ])
        for i in tropical_system.keys():                            # i = Name of species tropicalized
           all_variables = [] 
           count = 0
           monomials = []
           vertical = []
           mols_time = numpy.zeros(20000)
           plt.figure(1)
           plt.subplot(311)
           yuju = tropical_system[i].as_coefficients_dict().keys() # List of monomials of tropical equation tropical_system[i]
           for q, j in enumerate(yuju):                            #j is a monomial of tropical_system[i]
               monomials.append(str(j).partition('*Heaviside')[0])
               y_pos = numpy.arange(1,len(monomials)+1, 1)
               count = len(monomials)
               arg_f1 = []
               for par in self.model.parameters: j = sympy.simplify(j.subs(par.name, par.value))
               var_to_study = [atom for atom in j.atoms(sympy.Symbol) if not re.match(r'\d',str(atom))] #Variables of monomial 
               all_variables.append(var_to_study)
               f1 = sympy.lambdify(var_to_study, j, modules = dict(Heaviside=Heaviside_num, log=numpy.log, Abs=numpy.abs)) 
               for va in var_to_study:
                   arg_f1.append(y[:][str(va)])    
               x_concentration = numpy.nonzero(f1(*arg_f1))[0].tolist() # Gives the positions of nonzero numbers
               if len(x_concentration) > 0: vertical.append(x_concentration[-1])
               for ij in range(len(x_concentration)-1):
                   if x_concentration[ij] == x_concentration[ij+1]-1:
                      pass
                   else: vertical.append(x_concentration[ij])
               mols_time = mols_time + f1(*arg_f1)
               x_points = [t[x] for x in x_concentration] 
               prueba_y = numpy.repeat(count, len(x_points))
               plt.scatter(x_points, prueba_y, color = next(colors) )
               plt.xlim(0, t[len(t)-1])
               plt.ylim(0, len(yuju)+1)
               plt.title('Tropicalization' + ' ' + str(self.model.species[i]) )               
           plt.yticks(y_pos, monomials)
           
           plt.subplot(312)
           
           plt.plot(t[1:], mols_time, '*-')
           for i in vertical:
               plt.axvline(x=i, color = 'r')     
                                
           for ii in monomials:
               arg_test = []
               test = sympy.sympify(ii)
               for par in self.model.parameters: test = sympy.simplify(test.subs(par.name, par.value))
               var_test = [atom for atom in test.atoms(sympy.Symbol) if not re.match(r'\d',str(atom))]
               print test, var_test
               f_test = sympy.lambdify(var_test, test, 'numpy')
               for v in var_test:
                   arg_test.append(y[:][str(v)])
               plt.plot(t[1:], f_test(*arg_test))               
           plt.xlabel('t') #time
           plt.ylabel('ms/s') #molecules/second

           
           plt.subplot(313)
           all_variables_ready = list(set([item for sublist in all_variables for item in sublist]))
           for w in all_variables_ready:
               plt.plot(t[1:],y[:][str(w)], label=str(w) + '=' + str(model.species[int(re.findall(r'[0-9]', str(w))[0])]) )
               plt.xlim(0, t[len(t)-1])
               plt.xlabel('t') #time
               plt.ylabel('ms') #Number of molecules
               for i in vertical:
                   plt.axvline(x=i, color = 'r')
                   lgd = plt.legend(bbox_to_anchor=(0.5,-0.5), loc='lower center', ncol=3)
           plt.show()
#           plt.savefig('s%d'%i, bbox_extra_artists=(lgd,), bbox_inches='tight', dpi=800)      
           plt.clf()

# This prints all the species in a model
#         for i in range(len(self.model.species)):
#             plt.plot(self.t[10:],y[:]['s%d'%i], label=str(self.model.species[i]))
#             plt.xlabel('time')
#             plt.ylabel('Number of molecules')
#             plt.title('Tyson cycle')
#         plt.show()
             


# from pysb.examples.tyson_oscillator import model
# # from earm.lopez_embedded import model
# t= numpy.linspace(0, 20000, 20001)          # timerange used
# tro = Tropical(model)
# tro.tropicalize(t)
#tro.final_tropicalization() 

######################################################################## Change of parameters
"""
#tro.final_tropicalization()
rate_params = model.parameters_rules()
param_values = numpy.array([p.value for p in model.parameters])
rate_mask = numpy.array([p in rate_params for p in model.parameters])
k_ids = [p.value for p in model.parameters_rules()]
position = numpy.log10(param_values[rate_mask])
t=  numpy.linspace(0, 100, 1001)
import pysb
solver = pysb.integrate.Solver(model,t,integrator='vode')
solver.run(param_values)
plt.figure(1)
plt.subplot(212)
plt.plot(t,solver.y[:,5],'o-',label = 'before')
nummol = numpy.copy(solver.y[40:41].T.reshape(6))
for i in numpy.linspace(0.1,1,5):

    #Y=np.copy(x)
    #param_values[rate_mask] = 10 ** Y

    rate_params = model.parameters_rules()
    param_values = numpy.array([p.value for p in model.parameters])
    rate_mask = numpy.array([p in rate_params for p in model.parameters])
    k_ids = [p.value for p in model.parameters_rules()]
    param_values[6] = param_values[6]*i

    solver.run(param_values,y0=nummol)
    plt.plot(t[41:],solver.y[:-41,5],'x-',label=str(i))
    plt.legend(loc=0)
    plt.tight_layout()
    plt.title('k4p')
    #plt.b
plt.show()
"""