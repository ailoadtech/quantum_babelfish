from pyquil import Program
from pyquil.gates import *
p = Program()
p += H(0)
p += CNOT(0, 1)
