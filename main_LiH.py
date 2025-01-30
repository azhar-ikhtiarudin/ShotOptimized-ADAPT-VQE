from src.pools import QE
from src.molecules import create_h2, create_h3, create_lih

from algorithms.adapt_vqe_v4 import AdaptVQE


if __name__ == '__main__':    
    r = 1.45
    molecule = create_lih(r)
#     print(molecule.__dict__)
    print(molecule.fci_energy)
    pool = QE(molecule=None,
            frozen_orbitals=[],
            n=4)

    adapt_vqe = AdaptVQE(pool=pool,
                        molecule=None,
                        max_adapt_iter=50,
                        max_opt_iter=100,
                        grad_threshold=1e-8,
                        vrb=True,
                        optimizer_method='l-bfgs-b',
                        shots_assignment='uniform',
                        k=100,
                        shots_budget=10240,
			N_experiments=10
                        )

    adapt_vqe.run()
