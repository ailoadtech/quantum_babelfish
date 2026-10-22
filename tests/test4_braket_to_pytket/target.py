from pytket import Circuit, OpType

circ = Circuit(2, 2)
circ.add_gate(OpType.H, [0])
circ.add_gate(OpType.CX, [0, 1])
