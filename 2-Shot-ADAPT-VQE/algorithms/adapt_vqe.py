from copy import copy, deepcopy
import numpy as np
from scipy.sparse import csc_matrix

from .adapt_data import AdaptData
from src.minimize import minimize_bfgs
from src.utilities import ket_to_vector
from src.circuits import pauli_exp_circuit

from openfermion.transforms import jordan_wigner
from openfermion.utils import commutator

from qiskit import QuantumCircuit
from qiskit_aer import AerSimulator
from qiskit_ibm_runtime import EstimatorV2, Session

from scipy.optimize import minimize
from src.utilities import to_qiskit_operator

class AdaptVQE():
    """
        Main Class for ADAPT-VQE Algorithm
    """

    def __init__(self, pool, molecule, max_adapt_iter, max_opt_iter, grad_threshold=10**-8, vrb=False):
        self.pool = pool
        self.molecule = molecule
        self.max_adapt_iter = max_adapt_iter
        self.max_opt_iter = max_opt_iter
        self.vrb = vrb
        self.grad_threshold = grad_threshold

        self.data = None

        self.n = self.molecule.n_qubits
        self.fermionic_hamiltonian = self.molecule.get_molecular_hamiltonian()
        self.qubit_hamiltonian = jordan_wigner(self.fermionic_hamiltonian)
        self.qiskit_hamiltonian = to_qiskit_operator(self.qubit_hamiltonian)
        self.exact_energy = self.molecule.fci_energy
        self.window = 1

        # Hartree Fock Reference State:
        self.ref_determinant = [ 1 for _ in range(self.molecule.n_electrons) ]
        self.ref_determinant += [ 0 for _ in range(self.fermionic_hamiltonian.n_qubits - self.molecule.n_electrons ) ]

        self.sparse_ref_state = csc_matrix(
            ket_to_vector(self.ref_determinant), dtype = complex
            ).transpose()
        
        self.gradients = np.array(())

        if self.vrb:
            print(". . . == ADAPT-VQE Settings == . . .")
            print("Fermionic Hamiltonian:", self.fermionic_hamiltonian)
            print("Qubit Hamiltonian:", self.qubit_hamiltonian)
            print("Hartree Fock Reference State:", self.ref_determinant)


    def run(self):
        if self.vrb: print("\nRun, Initializing . . .")
        self.initialize()

        finished = False
        while not finished and self.data.iteration_counter < self.max_adapt_iter:
            finished = self.run_iteration()
        
        if not finished:
            viable_candidates, viable_gradients, total_norm, max_norm = (self.rank_gradients())
            if total_norm < self.grad_threshold:
                self.data.close(True, self.file_name) # converge()
                finished = True
        
        if finished:
            print("Converged")
            error = self.energy - self.exact_energy
        else:
            print("Maximum iteration reached before converged!")
            self.data.close(False)
            


    def initialize(self):
        print("Data:",self.data)
        if not self.data:
            print("Initializing Data . . .")
            self.indices = []
            self.coefficients = []
            self.old_coefficients = []
            self.old_gradients = []

        self.initial_energy = self.evaluate_observable(self.qubit_hamiltonian) 
        self.energy = self.initial_energy

        if not self.data: self.data = AdaptData(self.initial_energy, self.pool, self.exact_energy, self.n)
        return


    def run_iteration(self):
        if self.vrb: print("ADAPT-VQE Run Iteration")

        # Gradient Screening
        finished, viable_candidates, viable_gradients, total_norm = ( self.start_iteration() )
        if self.vrb: print("Viable Candidates:", viable_candidates, "Finished:", finished)

        if finished: return finished


        while viable_candidates:
            energy, gradient, viable_candidates, viable_gradients = self.grow_and_update( 
                viable_candidates, viable_gradients 
            )
            print("Energy:", energy, "gradient:", gradient)
        
        if energy is None: 
            energy = self.optimize(gradient) # Optimize energy with current updated ansatz (additional gradient g)

        self.complete_iteration(energy, total_norm, self.iteration_sel_gradients)

        return finished

    
    def start_iteration(self):
        if self.vrb: print("\n\n. . . === ADAPT-VQE Iteration", self.data.iteration_counter + 1, "=== . . .")

        viable_candidates, viable_gradients, total_norm, max_norm = ( self.rank_gradients() )

        finished = False
        if total_norm < self.grad_threshold:
            self.data.close(True, self.file_name) # converge()
            finished = True
        
        if finished: return finished, viable_candidates, viable_gradients, total_norm

        print(
            f"Operators under consideration ({len(viable_gradients)}):\n{viable_candidates}"
            f"\nCorresponding gradients (ordered by magnitude):\n{viable_gradients}"
        )

        self.iteration_nfevs = []
        self.iteration_ngevs = []
        self.iteration_nits = []
        self.iteration_sel_gradients = []
        self.iteration_qubits = ( set() )

        return finished, viable_candidates, viable_gradients, total_norm
        


    def rank_gradients(self, coefficients=None, indices=None):
        sel_gradients = []
        sel_indices = []
        total_norm = 0
        if self.vrb: print("Total Norm Initial:", total_norm)

        if self.vrb: print("-Pool Size:", self.pool.size)

        for index in range(self.pool.size):
            if self.vrb: print("\n===================== Evaluating Gradient", index, "=====================")

            gradient = self.eval_candidate_gradient(index, coefficients, indices)
            if self.vrb: print("-Gradient:", gradient)

            if np.abs(gradient) < 10**-8: continue

            sel_gradients, sel_indices = self.place_gradient( gradient, index, sel_gradients, sel_indices )

            print("Selected Gradients:", sel_gradients)
            print("Parent Range:", self.pool.parent_range)

            if index not in self.pool.parent_range: 
                print("---", index, self.pool.parent_range)
                print("Gradient before add to norm:", total_norm, gradient)
                total_norm += gradient**2
                print("___total norm:", total_norm, "___gradient:", gradient**2)

        total_norm = np.sqrt(total_norm)

        if sel_gradients: max_norm = sel_gradients[0]
        else: max_norm = 0

        if self.vrb:
            print("\n========== GRADIENT RANK RESULTS ========== ")
            print("Total gradient norm: {}".format(total_norm))
            print("Final Selected Indices:", sel_indices)
            print("Final Selected Gradients:", sel_gradients)
            print("Total Norm", total_norm)
            print("Max Norm", max_norm)
        
        return sel_indices, sel_gradients, total_norm, max_norm
    
    
    def eval_candidate_gradient(self, index, coefficients=None, indices=None):
        measurement = self.pool.get_grad_meas(index)

        if measurement is None:
            operator = self.pool.get_q_op(index)
            observable = commutator(self.qubit_hamiltonian, operator)
            
            if self.vrb: 
                print("Operator", self.pool.get_q_op(index))
                print("Observable", observable)
            
            self.pool.store_grad_meas(index, measurement)
        
        gradient = self.evaluate_observable(observable, coefficients, indices)

        return gradient


    def evaluate_observable(self, observable, coefficients=None, indices=None):
        qiskit_observable = to_qiskit_operator(observable)
        if self.vrb: print("\n---Qiskit Observable:", qiskit_observable)
        print("\n=== Qiskit Observable Measurement ===")

        # Obtain Quantum Circuit
        # qc = QuantumCircuit(self.n)
        # print("\n",self.ref_determinant)

        # for i, qubit in enumerate(self.ref_determinant):
        #     # print("i:",i,"qubit:",qubit)
        #     if qubit == 1: qc.x(i)
        
        # print(qc)
        # print("coefficients:",coefficients,", indices:", indices)
        # print("FCI Energy", self.molecule.fci_energy)
        # if indices is not None and coefficients is not None:
        #     qc = self.pool.get_circuit(indices, coefficients)
        
        # if self.vrb: print("Quantum Circuit:", qc)
        print("self.coefficients",self.coefficients)

        qc = self.get_quantum_circuit(self.ref_determinant, self.coefficients, self.indices)

        estimator = EstimatorV2(backend=AerSimulator())
        job = estimator.run([(qc, qiskit_observable)])
        exp_vals = job.result()[0].data.evs
        print("EXPECTATION VALUES", exp_vals)
        return exp_vals

    def get_quantum_circuit(self, ref_state, coefficients, indices):
        qc = QuantumCircuit(self.n)

        # Reference State
        for i, qubit in enumerate(ref_state):
            if qubit == 1 : qc.x(i)
        print("---Reference State:", qc)
        
        # Add Ansatz Operator specified by coefficients and indices
        if indices is not None and coefficients is not None:
            for i, (index, coefficient) in enumerate(zip(indices, coefficients)):
                qubit_operator = coefficient * self.pool.operators[index].q_operator
                qc_i = pauli_exp_circuit(qubit_operator, self.n, True)
                qc = qc.compose(qc_i)
                qc.barrier() 
        print("---Final State:", qc)
        return qc

    def place_gradient(self, gradient, index, sel_gradients, sel_indices):

        i = 0

        for sel_gradient in sel_gradients:
            if np.abs(np.abs(gradient) - np.abs(sel_gradient)) < self.grad_threshold:
                condition = self.break_gradient_tie(gradient, sel_gradient)
                if condition: break
            
            elif np.abs(gradient) - np.abs(sel_gradient) >= self.grad_threshold:
                break

            i += 1
        
        if i < self.window:
            sel_indices = sel_indices[:i] + [index] + sel_indices[i : self.window - 1]

            sel_gradients = (
                sel_gradients[:i] + [gradient] + sel_gradients[i : self.window - 1]
            )
        
        return sel_gradients, sel_indices

    def break_gradient_tie(self, gradient, sel_gradient):
        assert np.abs(np.abs(gradient) - np.abs(sel_gradient)) < self.grad_threshold

        # if self.rand_degenerate:
        #     # Position before/after with 50% probability
        #     condition = np.random.rand() < 0.5
        # else:
            # Just place the highest first even if the difference is small
        condition = np.abs(gradient) > np.abs(sel_gradient)

        return condition
    

    def grow_and_update(self, viable_candidates, viable_gradients):
        
        energy, gradient = self.grow_ansatz(viable_candidates, viable_gradients)

        viable_candidates, viable_gradients, extra_ngevs = (
            self.update_viable_candidates(viable_candidates, viable_gradients)
        )

        self.iteration_sel_gradients = np.append(self.iteration_sel_gradients, gradient)
        return energy, gradient, viable_candidates, viable_gradients


    def grow_ansatz(self, viable_candidates, viable_gradients, max_additions=1):

        total_new_nfevs = []
        total_new_ngevs = []
        total_new_nits = []
        gradients = []

        while max_additions > 0:
            if self.vrb: print("Max Additions:", max_additions)
            energy, gradient, new_nfevs, new_ngevs, new_nits = self.select_operators(
                viable_candidates, viable_gradients
            )

            if self.data.evolution.indices:
                old_size = len(self.data.evolution.indices[-1])
            else:
                old_size = 0
            new_indices = self.indices[old_size:]

            if new_nfevs:
                total_new_nfevs.append(new_nfevs)
            if new_ngevs:
                total_new_ngevs.append(new_nits)
            if new_nits:
                total_new_nits.append(new_nits)
            
            gradients.append(gradient)
            max_additions -= 1
        
        print("Operator(s) added to ansatz:", new_indices)
        self.update_iteration_costs(total_new_nfevs, total_new_ngevs, total_new_nits)

        return energy, gradient

    def select_operators(self, max_indices, max_gradients):
        
        new_nfevs = []
        new_ngevs = []
        new_nits = []
        energy = None

        gradient = self.select_via_gradient(max_indices, max_gradients)

        return energy, gradient, new_nfevs, new_ngevs, new_nits
    
    def select_via_gradient(self, indices, gradients):

        index, gradient = self.find_highest_gradient(indices, gradients)

        # Grow the ansatz and the parameter and gradient vectors
        self.indices.append(index)
        self.coefficients.append(0)
        self.gradients = np.append(self.gradients, gradient)

        return gradient
    
    def find_highest_gradient(self, indices, gradients, excluded_range=[]):

        viable_indices = []
        viable_gradients = []
        for index, gradient in zip(indices, gradients):
            if index not in excluded_range:
                viable_indices.append(index)
                viable_gradients.append(gradient)
        
        abs_gradients = [ np.abs(gradient) for gradient in viable_gradients ]
        max_abs_gradient = max(abs_gradients)

        grad_rank = abs_gradients.index(max_abs_gradient)
        index = viable_indices[grad_rank]
        gradient = viable_gradients[grad_rank]

        return index, gradient
    
    def update_iteration_costs(self, new_nfevs=None, new_ngevs=None, new_nits=None):
        if new_nfevs:
            self.iteration_nfevs = self.iteration_nfevs + new_nfevs
        if new_ngevs:
            self.iteration_ngevs = self.iteration_ngevs + new_ngevs
        if new_nits:
            self.iteration_nits = self.iteration_nits + new_nits

    def update_viable_candidates(self, viable_candidates, viable_gradients):
        viable_candidates = []
        ngevs = 0
        return viable_candidates, viable_gradients, ngevs
    

    def complete_iteration(self, energy, total_norm, sel_gradients):
        """
        Complete iteration by storing the relevant data and updating the state.

        Arguments:
            energy (float): final energy for this iteration
            total_norm (float): final gradient norm for this iteration
            sel_gradients(list): gradients selected in this iteration
        """

        energy_change = energy - self.energy
        self.energy = energy

        # Save iteration data
        self.data.process_iteration(
            self.indices,
            self.energy,
            total_norm,
            sel_gradients,
            self.coefficients,
            self.inv_hessian,
            self.gradients,
            self.iteration_nfevs,
            self.iteration_ngevs,
            self.iteration_nits,
        )

        # Update current state
        self.state = self.compute_state()

        print("\nCurrent energy:", self.energy)
        print(f"(change of {energy_change})")
        print(f"Current ansatz: {list(self.indices)}")

        return
    
    def optimize(self, gradient):
        """gradient: gradient of the last-added operator"""

        # Full Optimization
        print("\n\n. . . === Full Optimization === . . .")
        initial_coefficients = deepcopy(self.coefficients)
        indices = self.indices.copy()
        g0 = self.gradients
        e0 = self.energy
        maxiters = self.max_opt_iter

        print("\n## Energy Optimization Parameter")
        print("Initial Coefficients:", initial_coefficients)
        print("Indices:", indices)
        print("g0:", g0)
        print("e0:", e0, "\n")
        qc = self.pool.get_circuit(indices, initial_coefficients)
        print("Ansatz Circuit:", qc)

        print(
            f"\nInitial energy: {self.energy}"
            f"\nOptimizing energy with indices {list(indices)}..."
            f"\nStarting point: {list(initial_coefficients)}"
        )

        evolution = {
            "parameters":[],
            "energy":[],
            "gradient":[]
        }

        cost_history_dict = {
            "prev_vector": None,
            "iters":0,
            "cost_history":[]
        }

        print("Number of Parameters", qc.num_parameters)

        def cost_function(params, ansatz, hamiltonian, estimator):
            """Return estimate of energy from estimator"""

            pub = (ansatz, [hamiltonian], [params])
            result = estimator.run(pubs=[pub]).result()
            energy = result[0].data.evs[0]

            cost_history_dict['iters'] += 1
            cost_history_dict['previous_vector'] = params
            cost_history_dict['cost_history'].append(energy)

            print("Iterations done: ", cost_history_dict['iters'], "Current cost:", energy)
            return energy, result

        backend = AerSimulator()
        with Session(backend=backend) as session:
            estimator = EstimatorV2(session=session)
            estimator.options.default_shots = 1000

            res = minimize(
                cost_function,
                initial_coefficients,
                args=(qc, self.qiskit_hamiltonian, estimator, cost_history_dict)
            )


        # def callback(args):
        #     evolution['parameters'].append(args.x)
        #     evolution['energy'].append(args.fun)
        #     evolution['gradient'].append(args.gradient)
        
        # e_fun = lambda x, ixs: self.evaluate_energy(
        #     coefficients=x,
        #     indices=ixs
        # )

        # # extra_njev = 0

        # opt_result = minimize_bfgs(
        #     e_fun,
        #     initial_coefficients,
        #     [indices],
        #     jac=self.estimate_gradients,
        #     vrb=self.vrb,
        #     tolerance = 10**-8,
        #     maxiter = self.max_opt_iter,
        #     callback=callback,
        #     f0=e0,
        #     g0=g0
        # )

        # opt_coefficients = list(opt_result.x)
        # ansatz_coefficients = opt_coefficients
        # opt_energy = self.evaluate_observable(self.qubit_hamiltonian, ansatz_coefficients, indices)

        # nfev = opt_result.nfev
        # njev = opt_result.njev

        # ngev = njev * len(indices)
        # nit = opt_result.nit

        # gradients = evolution['gradient'][-1] if opt_result.nit else g0


        # self.coefficients = ansatz_coefficients
        # self.gradients = gradients
        # self.iteration_nfevs.append(nfev)
        # self.iteration_ngevs.append(ngev)
        # self.iteration_nits.append(nit)


        return opt_energy


    def estimate_gradients(
        self, coefficients=None, indices=None, method="an", dx=10**-8, orb_params=None
    ):
        """
        Estimates the gradients of all operators in the ansatz defined by coefficients and indices. If they are None,
        the current state is assumed. Default method is analytical (with unitary recycling for faster execution).

        Args:
            coefficients (list): the coefficients of the ansatz. If None, current coefficients will be used.
            indices (list): the indices of the ansatz. If None, current indices will be used.
            method (str): the method for estimating the gradient
            dx (float): the step size used for the finite difference approximation
            orb_params (list): the parameters for the orbital optimization, if applicable

        Returns:
            gradients (list): the gradient vector
        """

        if method == "fd":
            # Finite differences are implemented in parent class
            return super().estimate_gradients(
                coefficients=coefficients, indices=indices, method=method, dx=dx
            )

        if method != "an":
            raise ValueError(f"Method {method} is not supported.")

        if indices is None:
            assert coefficients is None
            indices = self.indices
            coefficients = self.coefficients

        # if self.orb_opt:
        #     orb_params = coefficients[: self.orb_opt_dim]
        #     coefficients = coefficients[self.orb_opt_dim :]
        # else:
        #     orb_params = None

        if not len(indices):
            return []

        # Define orbital rotation
        hamiltonian = self.fermionic_hamiltonian
        if orb_params is not None:
            generator = self.create_orb_rotation_generator(orb_params)
            orb_rotation = expm(generator)
            hamiltonian = (
                orb_rotation.transpose().conj().dot(hamiltonian).dot(orb_rotation)
            )
        else:
            orb_rotation = np.eye(2**self.n)
            orb_rotation = csc_matrix(orb_rotation)

        gradients = []
        state = self.compute_state(coefficients, indices)
        right_matrix = self.sparse_ref_state
        left_matrix = self.compute_state(
            coefficients, indices, hamiltonian.dot(state), bra=True
        )

        # Ansatz gradients
        for operator_pos in range(len(indices)):
            operator = self.pool.get_imp_op(indices[operator_pos])
            coefficient = coefficients[operator_pos]
            index = indices[operator_pos]

            left_matrix = (
                self.pool.expm_mult(coefficient, index, left_matrix.transpose().conj())
                .transpose()
                .conj()
            )
            right_matrix = self.pool.expm_mult(coefficient, index, right_matrix)

            gradient = 2 * (left_matrix.dot(operator.dot(right_matrix)))[0, 0].real
            gradients.append(gradient)

        right_matrix = csc_matrix(orb_rotation.dot(right_matrix))
        left_matrix = csc_matrix(right_matrix.transpose().conj())

        # Orbital gradients
        orb_gradients = []
        for operator in self.sparse_orb_ops:
            gradient = (
                2
                * left_matrix.dot(self.hamiltonian)
                .dot(operator)
                .dot(right_matrix)[0, 0]
                .real
            )
            orb_gradients.append(gradient)

        # Remember that orbital optimization coefficients come first
        gradients = orb_gradients + gradients

        return gradients
    
    def evaluate_energy(self, coefficients=None, indices=None):
        energy = self.evaluate_observable(self.qubit_hamiltonian, coefficients, indices)
        print("After Evaluate Energy:", energy)
        return energy
    
    def compute_state(self, coefficients=None, indices=None, ref_state=None, bra=False):
        if indices is None:
            indices = self.indices
        if coefficients is None:
            coefficients = self.coefficients
        
        if ref_state is None:
            ref_state = self.sparse_ref_state
        state = ref_state.copy()

        if bra:
            coefficients = [-c for c in reversed(coefficients)]
            indices = reversed(indices)
        
        for coefficient, index in zip(coefficients, indices):
            state = self.pool.expm_mult(coefficient, index, state)
        if bra:
            state = state.transpose().conj()
        
        return state
    