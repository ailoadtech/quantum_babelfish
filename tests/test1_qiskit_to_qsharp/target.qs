namespace Quantum.Program {
    open Microsoft.Quantum.Measurement;
    open Microsoft.Quantum.Intrinsic;

    operation RunCircuit() : Result[] {
        use qs = Qubit[2];
        H(qs[0]);
        CNOT(qs[0], qs[1]);
        let results = [M(qs[0]), M(qs[1])];
        ResetAll(qs);
        return results;
    }
}
