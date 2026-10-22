#!/usr/bin/env bash
set -e

echo "Running qbabel test suite..."

echo "[1/5] Translating Qiskit to Q#"
../qbabel --from qiskit --fromfile test1_qiskit_to_qsharp/source.py --to qsharp --tofile test1_qiskit_to_qsharp/target.qs --irfile test1_qiskit_to_qsharp/ir.json

echo "[2/5] Translating Cirq to OpenQASM"
../qbabel --from cirq --fromfile test2_cirq_to_openqasm/source.py --to openqasm --tofile test2_cirq_to_openqasm/target.qasm --irfile test2_cirq_to_openqasm/ir.json

echo "[3/5] Translating PennyLane to CUDA-Q"
../qbabel --from pennylane --fromfile test3_pennylane_to_cudaq/source.py --to cudaq --tofile test3_pennylane_to_cudaq/target.cpp --irfile test3_pennylane_to_cudaq/ir.json

echo "[4/5] Translating Amazon Braket to pytket"
../qbabel --from braket --fromfile test4_braket_to_pytket/source.py --to pytket --tofile test4_braket_to_pytket/target.py --irfile test4_braket_to_pytket/ir.json

echo "[5/5] Translating PyQuil to Qulacs"
../qbabel --from pyquil --fromfile test5_pyquil_to_qulacs/source.py --to qulacs --tofile test5_pyquil_to_qulacs/target.py --irfile test5_pyquil_to_qulacs/ir.json

echo "All tests completed successfully!"
