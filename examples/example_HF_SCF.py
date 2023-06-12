from functools import partial
from jax.random import PRNGKey
from jax.lax import stop_gradient
from evaluate import make_molecule_scf_loop

from interface.pyscf import molecule_from_pyscf
from molecule import dm21_combine, dm21_features
from functional import DM21

# First we define a molecule:
from pyscf import gto, dft
mol = gto.M(atom = 'H 0 0 0; F 0 0 1.1')

grids = dft.gen_grid.Grids(mol)
grids.level = 2
grids.build()

mf = dft.UKS(mol)
mf.grids = grids
ground_truth_energy = mf.kernel()

# Then we compute quite a few properties which we pack into a class called Molecule
molecule = molecule_from_pyscf(mf, omegas = [0., 0.4])

functional = DM21()
params = functional.generate_DM21_weights()

key = PRNGKey(42) # Jax-style random seed

# We generate the features from the molecule we created before
omegas = molecule.omegas
features_fn = dm21_features

functional_inputs = functional.features(molecule)
nograd_functional_inputs = stop_gradient(functional.nograd_features(molecule))
functional_inputs = functional.combine(functional_inputs, nograd_functional_inputs)

energy = functional.apply_and_integrate(params, molecule, *functional_inputs)
energy += molecule.nonXC()

# Alternatively, we can use an already prepared function that does everything
predicted_energy = functional.energy(params, molecule, *functional_inputs)
print('Predicted_energy:',predicted_energy)
# If we had a non-local functional, eg whose function f outputs an energy instead of an array,
# we'd just avoid the integrate step.

scf_iterator = make_molecule_scf_loop(functional, verbose = 2, functional_type = "DM21")
predicted_e = scf_iterator(params, molecule)

print(f'The predicted energy is {energy}')