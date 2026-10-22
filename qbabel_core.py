"""qbabel_core -- language-agnostic quantum program translation.

The pipeline is: source code --parse--> JSON IR --emit--> target code.

The IR is the JSON format described by ``quantum_schema.json``::

    {"ir_version": "1.0",
     "program": {"name": ..., "qubits": [{"id": "q0"}], "classical_bits": [...],
                 "operations": [{"type": "gate", "name": "CNOT",
                                 "controls": ["q0"], "targets": ["q1"]},
                                {"type": "measure", "qubits": ["q0"],
                                 "results": ["c0"]}]}}

Translation is best effort: gates a target language cannot express are emitted
as a comment and reported in ``warnings`` rather than silently dropped.
"""

import json
import re

IR_VERSION = "1.0"

SUPPORTED_LANGUAGES = [
    "qiskit", "cirq", "pennylane", "qsharp", "cudaq",
    "braket", "pytket", "openqasm", "pyquil", "qulacs",
]

# Language whose comment marker is "//" instead of "#".
SLASH_COMMENT_LANGS = {"qsharp", "cudaq", "openqasm"}

# Canonical gate names.  Anything else is reported as unrecognised.
ONE_QUBIT = ["H", "X", "Y", "Z", "S", "T", "Sdg", "Tdg", "SX"]
PARAM_ONE_QUBIT = ["RX", "RY", "RZ", "P"]
TWO_QUBIT = ["CNOT", "CZ", "SWAP"]
THREE_QUBIT = ["CCX"]


# --------------------------------------------------------------------------
# IR construction helpers
# --------------------------------------------------------------------------

def gate_op(name, targets, controls=(), params=()):
    op = {"type": "gate", "name": name,
          "targets": ["q%d" % int(t) for t in targets]}
    if controls:
        op["controls"] = ["q%d" % int(c) for c in controls]
    if params:
        op["parameters"] = [{"expression": _fmt_num(p), "type": "float"}
                            for p in params]
    return op


def measure_op(qubits):
    return {"type": "measure",
            "qubits": ["q%d" % int(q) for q in qubits],
            "results": ["c%d" % int(q) for q in qubits]}


def _fmt_num(value):
    text = str(value).strip()
    try:
        number = float(text)
    except ValueError:
        return text
    if number == int(number):
        return str(int(number))
    return repr(number)


def _index(token):
    """Pull a qubit index out of q0 / q[0] / qs[0] / LineQubit(0) / 0."""
    match = re.search(r"(\d+)", str(token))
    return int(match.group(1)) if match else None


class Builder:
    """Accumulates operations while tracking qubit usage and warnings."""

    def __init__(self):
        self.operations = []
        self.warnings = []
        self.nqubits = 0

    def touch(self, *indices):
        for index in indices:
            if index is not None:
                self.nqubits = max(self.nqubits, index + 1)

    def add(self, op):
        self.operations.append(op)
        return op

    def gate(self, name, targets, controls=(), params=()):
        controls = [c for c in controls if c is not None]
        targets = [t for t in targets if t is not None]
        if not targets and not controls:
            return None
        self.touch(*targets, *controls)
        return self.add(gate_op(name, targets, controls, params))

    def measure(self, qubits):
        qubits = [q for q in qubits if q is not None]
        if not qubits:
            return None
        self.touch(*qubits)
        return self.add(measure_op(qubits))

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def program(self, name, classical_bits):
        nqubits = max(self.nqubits, 1)
        classical = max(len(classical_bits), nqubits)
        return {
            "ir_version": IR_VERSION,
            "program": {
                "name": name,
                "qubits": [{"id": "q%d" % i} for i in range(nqubits)],
                "classical_bits": [{"id": "c%d" % i} for i in range(classical)],
                "operations": self.operations,
            },
        }


# --------------------------------------------------------------------------
# Source scanning helpers
# --------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
_STRONG_SIGNALS = re.compile(
    r"\b(cx|cnot|cz|swap|ccx|ccnot|toffoli|hadamard|paulix|pauliy|pauliz|"
    r"phaseshift|measure|measurement|mz|h_gate|x_gate|reset|add_\w+_gate)\b",
    re.IGNORECASE)
_SHORT_GATE_CALL = re.compile(r"\b([hxyzs]|rx|ry|rz|sdg|tdg|sx|p)\s*\(")


def strip_comments(line, lang):
    markers = ("//",) if lang in SLASH_COMMENT_LANGS else ("#",)
    out, quote, i = [], None, 0
    while i < len(line):
        char = line[i]
        if quote:
            out.append(char)
            if char == quote:
                quote = None
            i += 1
            continue
        if char in "'\"":
            quote = char
            out.append(char)
        elif any(line.startswith(marker, i) for marker in markers):
            break
        else:
            out.append(char)
        i += 1
    return "".join(out)


def logical_lines(source, lang):
    """Yield stripped, comment-free lines; joins lines with unbalanced parens."""
    buffer, depth = "", 0
    for raw in source.splitlines():
        line = strip_comments(raw, lang).strip()
        if not line:
            continue
        buffer = (buffer + " " + line).strip() if buffer else line
        depth = buffer.count("(") - buffer.count(")")
        if depth > 0:
            continue
        yield buffer
        buffer, depth = "", 0
    if buffer:
        yield buffer


def split_args(text):
    args, current, depth, quote = [], "", 0, None
    for char in text:
        if quote:
            current += char
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
            current += char
        elif char in "([{":
            depth += 1
            current += char
        elif char in ")]}":
            depth -= 1
            current += char
        elif char == "," and depth == 0:
            args.append(current.strip())
            current = ""
        else:
            current += char
    if current.strip():
        args.append(current.strip())
    return args


def _mask(text, spans):
    """Blank out already-consumed spans so later patterns cannot re-match them."""
    chars = list(text)
    for start, end in spans:
        for i in range(start, end):
            chars[i] = " "
    return "".join(chars)


def _call_args(text, open_index):
    """Return (inner_text, end_index) for the '(' at *open_index*, bracket aware."""
    depth, quote = 0, None
    for i in range(open_index, len(text)):
        char = text[i]
        if quote:
            if char == quote:
                quote = None
            continue
        if char in "'\"":
            quote = char
        elif char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
            if depth == 0:
                return text[open_index + 1:i], i + 1
    return text[open_index + 1:], len(text)


def _wires(text):
    match = re.search(r"wires\s*=\s*(\[[^\]]*\]|[^,)]+)", text)
    if not match:
        return []
    value = match.group(1).strip()
    if value.startswith("["):
        return [x.strip() for x in value[1:-1].split(",") if x.strip()]
    return [value]


def _numbers(text):
    return [int(n) for n in re.findall(r"\d+", text)]


def looks_quantum(line):
    words = {w.lower() for w in _WORD_RE.findall(line)}
    if words & {"cx", "cnot", "cz", "swap", "ccx", "ccnot", "toffoli",
                "hadamard", "paulix", "pauliy", "pauliz", "phaseshift",
                "measure", "measurement", "mz", "reset",
                "qubit", "qvector", "quantumregister"}:
        return True
    return bool(_SHORT_GATE_CALL.search(line))


_DECL_START = re.compile(
    r"^(import|from|open|namespace|operation|include|use|let|set|return|def|"
    r"struct|qubit\[|bit\[|qreg|creg|OPENQASM|@|end|barrier)\b")


def flag_unrecognised(line, b):
    stripped = line.strip()
    if _DECL_START.match(stripped):
        return
    if "=" in stripped.split("(")[0]:
        return
    if looks_quantum(stripped):
        b.warn("unrecognised source line, skipped: %s" % stripped)

# --------------------------------------------------------------------------
# Source parsers
# --------------------------------------------------------------------------

_QISKIT_ONE = {"h": "H", "x": "X", "y": "Y", "z": "Z", "s": "S", "t": "T",
               "sdg": "Sdg", "tdg": "Tdg", "sx": "SX"}
_QISKIT_PARAM = {"rx": "RX", "ry": "RY", "rz": "RZ", "p": "P", "phase": "P"}
_QISKIT_TWO = {"cx": "CNOT", "cnot": "CNOT", "cz": "CZ", "swap": "SWAP"}
_QISKIT_THREE = {"ccx": "CCX", "ccnot": "CCX", "toffoli": "CCX"}


def parse_qiskit(lines, b):
    for line in lines:
        for hint in re.finditer(r"QuantumCircuit\(([^)]*)\)", line):
            sizes = _numbers(hint.group(1))
            if sizes:
                b.nqubits = max(b.nqubits, sizes[0])
        consumed = False
        for match in re.finditer(r"(\w+)\.(\w+)\(([^()]*)\)", line):
            method = match.group(2).lower()
            raw = match.group(3)
            consumed = True
            if method == "measure":
                parts = split_args(raw)
                qubits = [_index(q) for q in _numbers(parts[0])] if parts else []
                if not qubits and parts:
                    qubits = [_index(parts[0])]
                b.measure(qubits)
            elif method in _QISKIT_ONE:
                args = split_args(raw)
                b.gate(_QISKIT_ONE[method], [_index(args[0])] if args else [])
            elif method in _QISKIT_PARAM:
                args = split_args(raw)
                if len(args) >= 2:
                    b.gate(_QISKIT_PARAM[method], [_index(args[1])], params=[args[0]])
            elif method in _QISKIT_TWO:
                args = split_args(raw)
                if len(args) >= 2:
                    first, second = _index(args[0]), _index(args[1])
                    if _QISKIT_TWO[method] == "CNOT":
                        b.gate("CNOT", [second], controls=[first])
                    else:
                        b.gate(_QISKIT_TWO[method], [first, second])
            elif method in _QISKIT_THREE:
                args = split_args(raw)
                if len(args) >= 3:
                    b.gate("CCX", [_index(args[2])],
                           controls=[_index(args[0]), _index(args[1])])
            elif method in ("barrier", "reset", "initialize", "id"):
                continue
            else:
                b.warn("unsupported qiskit method .%s(), skipped" % method)
        if not consumed:
            flag_unrecognised(line, b)


_CIRQ_ONE = {"h": "H", "x": "X", "y": "Y", "z": "Z", "s": "S", "t": "T",
             "sqrtx": "SX", "i": None}
_CIRQ_TWO = {"cnot": "CNOT", "cx": "CNOT", "cz": "CZ", "swap": "SWAP"}
_CIRQ_PARAM = {"rx": "RX", "ry": "RY", "rz": "RZ"}


def parse_cirq(lines, b):
    for line in lines:
        line = line.replace("cirq.Circuit(", "").replace("cirq.FrozenCircuit(", "")
        consumed = False
        for hint in re.finditer(r"LineQubit\.range\((\d+)\)", line):
            b.nqubits = max(b.nqubits, int(hint.group(1)))
        # one pass, in source order, so gate order is preserved
        for match in re.finditer(r"cirq\.(\w+)\s*\(([^()]*)\)\s*(?:\(([^()]*)\))?",
                                 line):
            name = match.group(1).lower()
            consumed = True
            if match.group(3) is not None:
                if name in _CIRQ_PARAM:
                    b.gate(_CIRQ_PARAM[name],
                           [_index(q) for q in split_args(match.group(3))],
                           params=[match.group(2)])
                else:
                    b.warn("unsupported cirq gate cirq.%s, skipped" % name)
                continue
            args = split_args(match.group(2))
            if name == "measure":
                b.measure([_index(a) for a in args if not a.startswith("key")])
            elif name in _CIRQ_ONE:
                if _CIRQ_ONE[name]:
                    b.gate(_CIRQ_ONE[name], [_index(a) for a in args])
            elif name in _CIRQ_TWO:
                first, second = _index(args[0]), _index(args[1])
                if _CIRQ_TWO[name] == "CNOT":
                    b.gate("CNOT", [second], controls=[first])
                else:
                    b.gate(_CIRQ_TWO[name], [first, second])
            elif name == "ccx":
                b.gate("CCX", [_index(args[2])],
                       controls=[_index(args[0]), _index(args[1])])
            elif name in ("linequbit", "gridqubit", "namedqubit", "circuit",
                          "moment", "measurementgate", "waitgate", "range"):
                continue
            else:
                b.warn("unsupported cirq gate cirq.%s(), skipped" % name)
        if not consumed:
            flag_unrecognised(line, b)

_PL_ONE = {"hadamard": "H", "paulix": "X", "pauliy": "Y", "pauliz": "Z",
           "s": "S", "t": "T", "sx": "SX"}
_PL_PARAM = {"rx": "RX", "ry": "RY", "rz": "RZ", "phaseshift": "P"}
_PL_TWO = {"cnot": "CNOT", "cx": "CNOT", "cz": "CZ", "swap": "SWAP",
           "iswap": None}
_PL_THREE = {"toffoli": "CCX", "ccx": "CCX"}


def parse_pennylane(lines, b):
    for line in lines:
        consumed = False
        for match in re.finditer(r"qml\.adjoint\(qml\.(\w+)\)\(([^)]*)\)", line):
            inner = match.group(1).lower()
            consumed = True
            base = _PL_ONE.get(inner)
            if base in ("S", "T"):
                b.gate(base + "dg", [_index(w) for w in _wires(match.group(2))])
            else:
                b.warn("unsupported pennylane adjoint of qml.%s, skipped" % inner)
        if consumed:
            continue
        for match in re.finditer(r"qml\.(\w+)\s*\(([^()]*)\)", line):
            name = match.group(1).lower()
            raw = match.group(2)
            consumed = True
            if name in ("device", "qnode", "state", "probs", "expval", "var",
                        "sample", "counts", "grad", "draw", "measure"):
                if name == "measure":
                    b.measure([_index(w) for w in _wires(raw)] or
                              [_index(a) for a in split_args(raw)])
                continue
            qubits = [_index(w) for w in _wires(raw)]
            if name in _PL_ONE:
                b.gate(_PL_ONE[name], qubits)
            elif name in _PL_PARAM:
                params = [a for a in split_args(raw) if "wires" not in a]
                b.gate(_PL_PARAM[name], qubits, params=params[:1])
            elif name in _PL_TWO:
                if _PL_TWO[name]:
                    if _PL_TWO[name] == "CNOT":
                        b.gate("CNOT", qubits[1:], controls=qubits[:1])
                    else:
                        b.gate(_PL_TWO[name], qubits)
            elif name in _PL_THREE:
                b.gate("CCX", qubits[2:], controls=qubits[:2])
            elif name in ("adjoint",):
                continue
            else:
                b.warn("unsupported pennylane op qml.%s(), skipped" % name)
        if not consumed:
            flag_unrecognised(line, b)


_QS_ONE = {"H": "H", "X": "X", "Y": "Y", "Z": "Z", "S": "S", "T": "T", "SX": "SX"}
_QS_PARAM = {"Rx": "RX", "Ry": "RY", "Rz": "RZ", "R1": "P"}
_QS_TWO = {"CNOT": "CNOT", "CZ": "CZ", "SWAP": "SWAP"}
_QS_SKIP = {"Qubit", "Result", "Int", "Double", "Bool", "Unit", "Range",
            "Reset", "ResetAll", "Message", "Length", "M", "MResetZ",
            "MResetX", "MResetY", "Let", "If", "Return", "Controlled", "Adjoint"}


def parse_qsharp(lines, b):
    for line in lines:
        if line.startswith("operation "):
            continue
        consumed = False
        for hint in re.finditer(r"Qubit\[(\d+)\]", line):
            b.nqubits = max(b.nqubits, int(hint.group(1)))
        for hint in re.finditer(r"use\s*\([^)]*\)\s*=", line):
            b.nqubits = max(b.nqubits, line.count("Qubit()"))
        spans = []
        for match in re.finditer(r"Adjoint\s+(\w+)\s*\(", line):
            name = match.group(1)
            inner, end = _call_args(line, match.end() - 1)
            consumed = True
            spans.append((match.start(), end))
            if name in ("S", "T"):
                b.gate(name + "dg", [_index(a) for a in split_args(inner)])
            else:
                b.warn("unsupported Q# Adjoint %s, skipped" % name)
        for match in re.finditer(r"Controlled\s+(\w+)\s*\(", line):
            name = match.group(1)
            inner, end = _call_args(line, match.end() - 1)
            consumed = True
            spans.append((match.start(), end))
            parts = split_args(inner)
            if len(parts) == 2 and parts[0].startswith("["):
                controls = [_index(c) for c in split_args(parts[0][1:-1])]
                target = _index(parts[1])
                if name == "X" and len(controls) <= 2:
                    b.gate("CNOT" if len(controls) == 1 else "CCX", [target],
                           controls=controls)
                else:
                    b.warn("unsupported Q# Controlled %s with %d controls, skipped"
                           % (name, len(controls)))
            else:
                b.warn("unsupported Q# Controlled %s, skipped" % name)
        for match in re.finditer(r"\bM\(([^)]*)\)", line):
            consumed = True
            spans.append(match.span())
            b.measure([_index(a) for a in split_args(match.group(1))])
        if spans:
            line = _mask(line, spans)
        for match in re.finditer(r"\b([A-Za-z]\w*)\s*\(([^()]*)\)", line):
            name = match.group(1)
            if name in _QS_SKIP or name == "M":
                continue
            args = split_args(match.group(2))
            consumed = True
            if name in _QS_ONE:
                b.gate(_QS_ONE[name], [_index(a) for a in args])
            elif name in _QS_PARAM:
                b.gate(_QS_PARAM[name], [_index(args[-1])], params=[args[0]])
            elif name in _QS_TWO:
                first, second = _index(args[0]), _index(args[1])
                if _QS_TWO[name] == "CNOT":
                    b.gate("CNOT", [second], controls=[first])
                else:
                    b.gate(_QS_TWO[name], [first, second])
            elif name in ("Reset", "ResetAll"):
                continue
            else:
                b.warn("unsupported Q# call %s(), skipped" % name)
        if not consumed:
            flag_unrecognised(line, b)

_CUDAQ_ONE = {"h": "H", "x": "X", "y": "Y", "z": "Z", "s": "S", "t": "T",
              "sdg": "Sdg", "tdg": "Tdg", "sx": "SX"}
_CUDAQ_PARAM = {"rx": "RX", "ry": "RY", "rz": "RZ", "r1": "P", "u1": "P"}
_CUDAQ_TWO = {"cx": "CNOT", "cnot": "CNOT", "cz": "CZ", "swap": "SWAP",
              "cy": None}
_CUDAQ_SKIP = {"cudaq", "kernel", "qvector", "qubit", "def", "print", "range",
               "len", "if", "for", "while", "return", "sample", "observe", "mz", }


def parse_cudaq(lines, b):
    for line in lines:
        if line.startswith("@") or line.startswith("def "):
            continue  # decorator and kernel declaration, not a gate
        line = line.replace("cudaq.qvector(", "qvector(")
        consumed = False
        for hint in re.finditer(r"qvector\((\d+)\)", line):
            b.nqubits = max(b.nqubits, int(hint.group(1)))
            consumed = True
        spans = []
        for match in re.finditer(r"(\w+)\.(ctrl|inv|adj)\s*\(([^()]*)\)", line):
            name = match.group(1).lower()
            consumed = True
            spans.append(match.span())
            args = [_index(a) for a in split_args(match.group(3))]
            if name == "x":
                b.gate("CNOT" if len(args) == 2 else "CCX", args[-1:],
                       controls=args[:-1])
            else:
                b.warn("unsupported cudaq call %s.%s(), skipped"
                       % (name, match.group(2)))
        for match in re.finditer(r"(\w+)\s*<[^>]*>\(([^)]*)\)", line):
            name = match.group(1).lower()
            args = [_index(a) for a in split_args(match.group(2))]
            consumed = True
            spans.append(match.span())
            if name == "x":
                b.gate("CNOT" if len(args) == 2 else "CCX", args[-1:],
                       controls=args[:-1])
            else:
                _cudaq_plain(name, args, b)
        for match in re.finditer(r"\bmz\(([^)]*)\)", line):
            consumed = True
            spans.append(match.span())
            b.measure([_index(a) for a in split_args(match.group(1))])
        if spans:
            line = _mask(line, spans)
        for match in re.finditer(r"(@?\w+)\s*\(([^()]*)\)", line):
            name = match.group(1).lower()
            if name in _CUDAQ_SKIP or name.startswith("def "):
                consumed = True
                continue
            args = split_args(match.group(2))
            consumed = True
            _cudaq_plain(name, args, b)
        if not consumed:
            flag_unrecognised(line, b)


def _cudaq_plain(name, args, b):
    if name == "x":
        if len(args) == 1:
            b.gate("X", [_index(args[0])])
        elif len(args) == 2:
            b.gate("CNOT", [_index(args[1])], controls=[_index(args[0])])
        else:
            b.gate("CCX", [_index(args[-1])],
                   controls=[_index(a) for a in args[:-1]][:2])
    elif name in _CUDAQ_ONE:
        b.gate(_CUDAQ_ONE[name], [_index(a) for a in args])
    elif name in _CUDAQ_PARAM:
        b.gate(_CUDAQ_PARAM[name], [_index(args[-1])], params=args[:-1][:1])
    elif name in _CUDAQ_TWO:
        if _CUDAQ_TWO[name] == "CNOT":
            b.gate("CNOT", [_index(args[1])], controls=[_index(args[0])])
        elif _CUDAQ_TWO[name] is None:
            b.warn("unsupported cudaq gate %s, skipped" % name)
        else:
            b.gate(_CUDAQ_TWO[name], [_index(a) for a in args])
    else:
        b.warn("unsupported cudaq call %s(), skipped" % name)

_BK_ONE = {"h": "H", "x": "X", "y": "Y", "z": "Z", "s": "S", "t": "T",
           "si": "Sdg", "ti": "Tdg", "v": "SX", "vi": None}
_BK_PARAM = {"rx": "RX", "ry": "RY", "rz": "RZ", "phaseshift": "P"}
_BK_TWO = {"cnot": "CNOT", "cz": "CZ", "swap": "SWAP"}
_BK_THREE = {"ccnot": "CCX"}


def parse_braket(lines, b):
    for line in lines:
        consumed = False
        for match in re.finditer(r"circ\.(\w+)\s*\(([^()]*)\)", line):
            name = match.group(1).lower()
            raw = match.group(2)
            args = split_args(raw)
            consumed = True
            if name == "measure":
                b.measure([_index(a) for a in args])
            elif name in _BK_ONE and _BK_ONE[name]:
                b.gate(_BK_ONE[name], [_index(args[0])])
            elif name in _BK_PARAM:
                b.gate(_BK_PARAM[name], [_index(args[0])], params=args[1:2])
            elif name in _BK_TWO:
                if "control=" in raw or "target=" in raw:
                    control = re.search(r"control\s*=\s*(\d+)", raw)
                    target = re.search(r"target\s*=\s*(\d+)", raw)
                    if control and target:
                        b.gate("CNOT", [int(target.group(1))],
                               controls=[int(control.group(1))])
                        continue
                first, second = _index(args[0]), _index(args[1])
                if _BK_TWO[name] == "CNOT":
                    b.gate("CNOT", [second], controls=[first])
                else:
                    b.gate(_BK_TWO[name], [first, second])
            elif name in _BK_THREE:
                b.gate("CCX", [_index(args[2])],
                       controls=[_index(args[0]), _index(args[1])])
            else:
                b.warn("unsupported braket gate circ.%s(), skipped" % name)
        if not consumed:
            flag_unrecognised(line, b)


_TK_ONE = {"H": "H", "X": "X", "Y": "Y", "Z": "Z", "S": "S", "T": "T",
           "Sdg": "Sdg", "Tdg": "Tdg", "V": "SX", "SX": "SX"}
_TK_PARAM = {"Rx": "RX", "Ry": "RY", "Rz": "RZ"}
_TK_TWO = {"CX": "CNOT", "CZ": "CZ", "SWAP": "SWAP"}
_TK_THREE = {"CCX": "CCX"}
_TK_SKIP = {"Circuit", "Op", "OpType", "add_gate", "measure", "Measure"}
_TK_OP_PARAM = {"Rx": "RX", "Ry": "RY", "Rz": "RZ"}


def parse_pytket(lines, b):
    for line in lines:
        for hint in re.finditer(r"Circuit\((\d+)", line):
            b.nqubits = max(b.nqubits, int(hint.group(1)))
        consumed = False
        spans = []
        for match in re.finditer(
                r"add_gate\(\s*OpType\.(?:\w+\.)?(\w+)\s*,\s*([0-9.]+)\s*,"
                r"\s*\[([^\]]*)\]", line):
            consumed = True
            spans.append(match.span())
            name = match.group(1)
            canonical = _TK_OP_PARAM.get(name)
            if canonical:
                b.gate(canonical, [_index(x) for x in split_args(match.group(3))],
                       params=[match.group(2)])
            else:
                b.warn("unsupported pytket OpType.%s, skipped" % name)
        for match in re.finditer(
                r"add_gate\(\s*OpType\.(?:\w+\.)?(\w+)\s*,\s*\[([^\]]*)\]",
                line):
            consumed = True
            spans.append(match.span())
            name = match.group(1)
            args = [_index(x) for x in split_args(match.group(2))]
            if name in _TK_ONE:
                b.gate(_TK_ONE[name], args)
            elif name in _TK_TWO:
                if _TK_TWO[name] == "CNOT":
                    b.gate("CNOT", args[1:], controls=args[:1])
                else:
                    b.gate(_TK_TWO[name], args)
            elif name in _TK_THREE:
                b.gate("CCX", args[2:], controls=args[:2])
            else:
                b.warn("unsupported pytket OpType.%s, skipped" % name)
        if spans:
            line = _mask(line, spans)
        for match in re.finditer(r"circ\.(\w+)\s*\(([^()]*)\)", line):
            name = match.group(1)
            args = split_args(match.group(2))
            consumed = True
            if name == "Measure":
                b.measure([_index(args[0])])
            elif name in _TK_ONE:
                b.gate(_TK_ONE[name], [_index(a) for a in args])
            elif name in _TK_PARAM:
                b.gate(_TK_PARAM[name], [_index(args[1])], params=args[:1])
            elif name in _TK_TWO:
                if _TK_TWO[name] == "CNOT":
                    b.gate("CNOT", [_index(args[1])], controls=[_index(args[0])])
                else:
                    b.gate(_TK_TWO[name], [_index(a) for a in args])
            elif name in _TK_THREE:
                b.gate("CCX", [_index(args[2])],
                       controls=[_index(args[0]), _index(args[1])])
            elif name in _TK_SKIP:
                continue
            else:
                b.warn("unsupported pytket gate circ.%s(), skipped" % name)
        if not consumed:
            flag_unrecognised(line, b)

_OQ_ONE = {"h": "H", "x": "X", "y": "Y", "z": "Z", "s": "S", "t": "T",
           "sdg": "Sdg", "tdg": "Tdg", "sx": "SX"}
_OQ_PARAM = {"rx": "RX", "ry": "RY", "rz": "RZ", "p": "P", "u1": "P"}
_OQ_TWO = {"cx": "CNOT", "cnot": "CNOT", "cz": "CZ", "swap": "SWAP"}
_OQ_THREE = {"ccx": "CCX"}


def parse_openqasm(lines, b):
    for line in lines:
        if line.startswith("OPENQASM") or line.startswith("include"):
            continue
        consumed = False
        spans = []
        for hint in re.finditer(r"qubit\[(\d+)\]|qreg\s+\w+\[(\d+)\]", line):
            b.nqubits = max(b.nqubits, int(hint.group(1) or hint.group(2)))
        for match in re.finditer(
                r"c\[(\d+)\]\s*(?:=|->)\s*measure\s+\w+\[(\d+)\]", line):
            consumed = True
            spans.append(match.span())
            b.measure([int(match.group(2))])
        for match in re.finditer(r"(\w+)\s*\(([^)]*)\)\s*q\[([\d,\s]+)\]", line):
            name = match.group(1).lower()
            if name in _OQ_PARAM:
                consumed = True
                spans.append(match.span())
                b.gate(_OQ_PARAM[name], [int(x) for x in _numbers(match.group(3))],
                       params=[match.group(2)])
            elif name in _OQ_ONE:
                consumed = True
                spans.append(match.span())
                b.gate(_OQ_ONE[name], [int(x) for x in _numbers(match.group(3))])
        if spans:
            line = _mask(line, spans)
        for match in re.finditer(r"\b(\w+)\s+((?:q\[\d+\]\s*,?\s*)+);", line):
            name = match.group(1).lower()
            args = [int(x) for x in _numbers(match.group(2))]
            if not args:
                continue
            consumed = True
            if name in _OQ_ONE:
                b.gate(_OQ_ONE[name], args)
            elif name in _OQ_TWO:
                if _OQ_TWO[name] == "CNOT":
                    b.gate("CNOT", args[1:], controls=args[:1])
                else:
                    b.gate(_OQ_TWO[name], args)
            elif name in _OQ_THREE:
                b.gate("CCX", args[2:], controls=args[:2])
            elif name in ("measure", "reset"):
                continue
            else:
                b.warn("unsupported OpenQASM gate %s, skipped" % name)
        if not consumed and re.search(r"q\[\d+\]", line):
            flag_unrecognised(line, b)

_PQ_ONE = {"H": "H", "X": "X", "Y": "Y", "Z": "Z", "S": "S", "T": "T"}
_PQ_PARAM = {"RX": "RX", "RY": "RY", "RZ": "RZ", "PHASE": "P"}
_PQ_TWO = {"CNOT": "CNOT", "CZ": "CZ", "SWAP": "SWAP"}
_PQ_THREE = {"CCNOT": "CCX", "TOFFOLI": "CCX"}


def parse_pyquil(lines, b):
    for line in lines:
        consumed = False
        for match in re.finditer(r"(?:\w+)?\s*\+=\s*(\w+)\s*\(([^()]*)\)", line):
            name = match.group(1)
            args = split_args(match.group(2))
            consumed = True
            if name == "MEASURE":
                b.measure([_index(args[0])])
            elif name in _PQ_ONE:
                b.gate(_PQ_ONE[name], [_index(args[0])])
            elif name in _PQ_PARAM:
                b.gate(_PQ_PARAM[name], [_index(args[-1])], params=args[:1])
            elif name in _PQ_TWO:
                if _PQ_TWO[name] == "CNOT":
                    b.gate("CNOT", [_index(args[1])], controls=[_index(args[0])])
                else:
                    b.gate(_PQ_TWO[name], [_index(a) for a in args])
            elif name in _PQ_THREE:
                b.gate("CCX", [_index(args[2])],
                       controls=[_index(args[0]), _index(args[1])])
            else:
                b.warn("unsupported pyquil gate %s(), skipped" % name)
        if not consumed:
            flag_unrecognised(line, b)


_QL_PARAM = {"RX": "RX", "RY": "RY", "RZ": "RZ", "U1": "P", "U2": None,
             "U3": None, "PHASE": "P"}
_QL_NOARG = {"H": "H", "X": "X", "Y": "Y", "Z": "Z", "S": "S", "T": "T",
             "SqrtX": "SX"}
_QL_TWO = {"CNOT": "CNOT", "CZ": "CZ", "SWAP": "SWAP"}


def parse_qulacs(lines, b):
    for line in lines:
        for hint in re.finditer(r"QuantumCircuit\((\d+)", line):
            b.nqubits = max(b.nqubits, int(hint.group(1)))
        consumed = False
        for match in re.finditer(r"add_(\w+?)_gate\s*\(([^()]*)\)", line):
            name = match.group(1)
            args = split_args(match.group(2))
            consumed = True
            if name in ("Sdag", "Tdag"):
                b.gate(name, [_index(args[0])])
            elif name in _QL_NOARG:
                b.gate(_QL_NOARG[name], [_index(a) for a in args])
            elif name in _QL_PARAM:
                if _QL_PARAM[name]:
                    b.gate(_QL_PARAM[name], [_index(args[0])], params=args[1:2])
                else:
                    b.warn("unsupported qulacs gate %s, skipped" % name)
            elif name in _QL_TWO:
                if _QL_TWO[name] == "CNOT":
                    b.gate("CNOT", [_index(args[1])], controls=[_index(args[0])])
                else:
                    b.gate(_QL_TWO[name], [_index(a) for a in args])
            elif name == "Toffoli":
                b.gate("CCX", [_index(args[2])],
                       controls=[_index(args[0]), _index(args[1])])
            else:
                b.warn("unsupported qulacs gate add_%s_gate, skipped" % name)
        if not consumed:
            flag_unrecognised(line, b)


PARSERS = {
    "qiskit": parse_qiskit,
    "cirq": parse_cirq,
    "pennylane": parse_pennylane,
    "qsharp": parse_qsharp,
    "cudaq": parse_cudaq,
    "braket": parse_braket,
    "pytket": parse_pytket,
    "openqasm": parse_openqasm,
    "pyquil": parse_pyquil,
    "qulacs": parse_qulacs,
}


def parse(source, lang, name="circuit"):
    """Parse *source* in *lang* and return (ir, warnings)."""
    lang = lang.lower()
    if lang not in PARSERS:
        raise ValueError("unsupported source language: %s" % lang)
    b = Builder()
    PARSERS[lang](logical_lines(source, lang), b)
    return b.program(name or ("from_%s" % lang), []), b.warnings


# --------------------------------------------------------------------------
# Target emitters
# --------------------------------------------------------------------------

def _cirq_labels(n):
    names = ", ".join("q%d" % i for i in range(n))
    if n == 1:
        names += ","
    return names


def _spec(qref, cref, comment, header, gates, measure=None, indent="",
          footer=(), measure_style="inline"):
    return {
        "qref": qref, "cref": cref, "comment": comment,
        "header": header, "gates": gates, "measure": measure,
        "indent": indent, "footer": list(footer), "measure_style": measure_style,
    }


LANGS = {
    "qiskit": _spec(
        lambda i: str(i), lambda i: str(i), "#",
        lambda n, m: ["from qiskit import QuantumCircuit",
                      "qc = QuantumCircuit(%d, %d)" % (n, m)],
        {"H": "qc.h({a})", "X": "qc.x({a})", "Y": "qc.y({a})", "Z": "qc.z({a})",
         "S": "qc.s({a})", "T": "qc.t({a})", "Sdg": "qc.sdg({a})",
         "Tdg": "qc.tdg({a})", "SX": "qc.sx({a})",
         "RX": "qc.rx({p}, {a})", "RY": "qc.ry({p}, {a})", "RZ": "qc.rz({p}, {a})",
         "P": "qc.p({p}, {a})",
         "CNOT": "qc.cx({c}, {t})", "CZ": "qc.cz({a}, {b})",
         "SWAP": "qc.swap({a}, {b})", "CCX": "qc.ccx({c1}, {c2}, {t})"},
        measure="qc.measure({a}, {k})"),

    "cirq": _spec(
        lambda i: "q%d" % i, lambda i: "c%d" % i, "#",
        lambda n, m: ["import cirq", "%s = cirq.LineQubit.range(%d)" % (_cirq_labels(n), n),
                      "", "circuit = cirq.Circuit("],
        {"H": "cirq.H({a}),", "X": "cirq.X({a}),", "Y": "cirq.Y({a}),",
         "Z": "cirq.Z({a}),", "S": "cirq.S({a}),", "T": "cirq.T({a}),",
         "Sdg": "cirq.S({a})**-1,", "Tdg": "cirq.T({a})**-1,",
         "SX": "cirq.X({a})**0.5,",
         "RX": "cirq.rx({p})({a}),", "RY": "cirq.ry({p})({a}),",
         "RZ": "cirq.rz({p})({a}),",
         "P": "cirq.Z({a})**({p}/3.141592653589793),",
         "CNOT": "cirq.CNOT({c}, {t}),", "CZ": "cirq.CZ({a}, {b}),",
         "SWAP": "cirq.SWAP({a}, {b}),", "CCX": "cirq.CCX({c1}, {c2}, {t}),"},
        measure="cirq.measure({a}, key='{k}'),",
        indent="    ", footer=[")"]),

    "pennylane": _spec(
        lambda i: str(i), lambda i: str(i), "#",
        lambda n, m: ["import pennylane as qml", "",
                      "dev = qml.device('default.qubit', wires=%d)" % n, "",
                      "@qml.qnode(dev)", "def circuit():"],
        {"H": "qml.Hadamard(wires={a})", "X": "qml.PauliX(wires={a})",
         "Y": "qml.PauliY(wires={a})", "Z": "qml.PauliZ(wires={a})",
         "S": "qml.S(wires={a})", "T": "qml.T(wires={a})",
         "Sdg": "qml.adjoint(qml.S)(wires={a})",
         "Tdg": "qml.adjoint(qml.T)(wires={a})",
         "SX": "qml.SX(wires={a})",
         "RX": "qml.RX({p}, wires={a})", "RY": "qml.RY({p}, wires={a})",
         "RZ": "qml.RZ({p}, wires={a})", "P": "qml.PhaseShift({p}, wires={a})",
         "CNOT": "qml.CNOT(wires=[{c}, {t}])", "CZ": "qml.CZ(wires=[{a}, {b}])",
         "SWAP": "qml.SWAP(wires=[{a}, {b}])",
         "CCX": "qml.Toffoli(wires=[{c1}, {c2}, {t}])"},
        measure="m{k} = qml.measure({a})",
        indent="    ", footer=["    return qml.state()"]),

    "qsharp": _spec(
        lambda i: "qs[%d]" % i, lambda i: "c%d" % i, "//",
        lambda n, m: ["namespace Quantum.Program {",
                      "    open Microsoft.Quantum.Measurement;",
                      "    open Microsoft.Quantum.Intrinsic;", "",
                      "    operation RunCircuit() : Result[] {",
                      "        use qs = Qubit[%d];" % n],
        {"H": "H({a});", "X": "X({a});", "Y": "Y({a});", "Z": "Z({a});",
         "S": "S({a});", "T": "T({a});", "Sdg": "Adjoint S({a});",
         "Tdg": "Adjoint T({a});", "SX": "SX({a});",
         "RX": "Rx({p}, {a});", "RY": "Ry({p}, {a});", "RZ": "Rz({p}, {a});",
         "P": "R1({p}, {a});",
         "CNOT": "CNOT({c}, {t});", "CZ": "CZ({a}, {b});",
         "SWAP": "SWAP({a}, {b});",
         "CCX": "Controlled X([{c1}, {c2}], {t});"},
        measure=None, indent="        ",
        footer=["        ResetAll(qs);", "        return results;", "    }", "}"],
        measure_style="qsharp"),

    "cudaq": _spec(
        lambda i: "q[%d]" % i, lambda i: "c%d" % i, "#",
        lambda n, m: ["import cudaq", "", "@cudaq.kernel", "def kernel():",
                      "    q = cudaq.qvector(%d)" % n],
        {"H": "h({a})", "X": "x({a})", "Y": "y({a})", "Z": "z({a})",
         "S": "s({a})", "T": "t({a})", "Sdg": "sdg({a})", "Tdg": "tdg({a})",
         "SX": "sx({a})",
         "RX": "rx({p}, {a})", "RY": "ry({p}, {a})", "RZ": "rz({p}, {a})",
         "P": "r1({p}, {a})",
         "CNOT": "x.ctrl({c}, {t})", "CZ": "cz({a}, {b})",
         "SWAP": "swap({a}, {b})", "CCX": "x.ctrl({c1}, {c2}, {t})"},
        measure="mz({a})", indent="    "),

    "braket": _spec(
        lambda i: str(i), lambda i: str(i), "#",
        lambda n, m: ["from braket.circuits import Circuit", "",
                      "circ = Circuit()"],
        {"H": "circ.h({a})", "X": "circ.x({a})", "Y": "circ.y({a})",
         "Z": "circ.z({a})", "S": "circ.s({a})", "T": "circ.t({a})",
         "Sdg": "circ.si({a})", "Tdg": "circ.ti({a})", "SX": "circ.v({a})",
         "RX": "circ.rx({a}, {p})", "RY": "circ.ry({a}, {p})",
         "RZ": "circ.rz({a}, {p})", "P": "circ.phaseshift({a}, {p})",
         "CNOT": "circ.cnot({c}, {t})", "CZ": "circ.cz({a}, {b})",
         "SWAP": "circ.swap({a}, {b})", "CCX": "circ.ccnot({c1}, {c2}, {t})"},
        measure=None),

    "pytket": _spec(
        lambda i: str(i), lambda i: str(i), "#",
        lambda n, m: ["from pytket import Circuit, OpType", "",
                      "circ = Circuit(%d, %d)" % (n, m)],
        {"H": "circ.add_gate(OpType.H, [{a}])", "X": "circ.add_gate(OpType.X, [{a}])",
         "Y": "circ.add_gate(OpType.Y, [{a}])", "Z": "circ.add_gate(OpType.Z, [{a}])",
         "S": "circ.add_gate(OpType.S, [{a}])", "T": "circ.add_gate(OpType.T, [{a}])",
         "Sdg": "circ.add_gate(OpType.Sdg, [{a}])",
         "Tdg": "circ.add_gate(OpType.Tdg, [{a}])",
         "SX": "circ.add_gate(OpType.SX, [{a}])",
         "RX": "circ.add_gate(OpType.Rx, {p}, [{a}])",
         "RY": "circ.add_gate(OpType.Ry, {p}, [{a}])",
         "RZ": "circ.add_gate(OpType.Rz, {p}, [{a}])",
         "P": "circ.add_gate(OpType.TK1, 0, {p}, [{a}])",
         "CNOT": "circ.add_gate(OpType.CX, [{c}, {t}])",
         "CZ": "circ.add_gate(OpType.CZ, [{a}, {b}])",
         "SWAP": "circ.add_gate(OpType.SWAP, [{a}, {b}])",
         "CCX": "circ.add_gate(OpType.CCX, [{c1}, {c2}, {t}])"},
        measure="circ.Measure({a}, {k})"),

    "openqasm": _spec(
        lambda i: "q[%d]" % i, lambda i: "c[%d]" % i, "//",
        lambda n, m: ["OPENQASM 3.0;", 'include "stdgates.inc";',
                      "qubit[%d] q;" % n, "bit[%d] c;" % m],
        {"H": "h {a};", "X": "x {a};", "Y": "y {a};", "Z": "z {a};",
         "S": "s {a};", "T": "t {a};", "Sdg": "sdg {a};", "Tdg": "tdg {a};",
         "SX": "sx {a};",
         "RX": "rx({p}) {a};", "RY": "ry({p}) {a};", "RZ": "rz({p}) {a};",
         "P": "p({p}) {a};",
         "CNOT": "cx {c}, {t};", "CZ": "cz {a}, {b};",
         "SWAP": "swap {a}, {b};", "CCX": "ccx {c1}, {c2}, {t};"},
        measure="{k} = measure {a};"),

    "pyquil": _spec(
        lambda i: str(i), lambda i: str(i), "#",
        lambda n, m: ["from pyquil import Program", "from pyquil.gates import *",
                      "", "p = Program()"],
        {"H": "p += H({a})", "X": "p += X({a})", "Y": "p += Y({a})",
         "Z": "p += Z({a})", "S": "p += S({a})", "T": "p += T({a})",
         "RX": "p += RX({p}, {a})", "RY": "p += RY({p}, {a})",
         "RZ": "p += RZ({p}, {a})", "P": "p += PHASE({p}, {a})",
         "CNOT": "p += CNOT({c}, {t})", "CZ": "p += CZ({a}, {b})",
         "SWAP": "p += SWAP({a}, {b})", "CCX": "p += CCNOT({c1}, {c2}, {t})"},
        measure="p += MEASURE({a}, {k})"),

    "qulacs": _spec(
        lambda i: str(i), lambda i: str(i), "#",
        lambda n, m: ["from qulacs import QuantumCircuit", "",
                      "circuit = QuantumCircuit(%d)" % n],
        {"H": "circuit.add_H_gate({a})", "X": "circuit.add_X_gate({a})",
         "Y": "circuit.add_Y_gate({a})", "Z": "circuit.add_Z_gate({a})",
         "S": "circuit.add_S_gate({a})", "T": "circuit.add_T_gate({a})",
         "Sdg": "circuit.add_Sdag_gate({a})", "Tdg": "circuit.add_Tdag_gate({a})",
         "SX": "circuit.add_SqrtX_gate({a})",
         "RX": "circuit.add_RX_gate({a}, {p})", "RY": "circuit.add_RY_gate({a}, {p})",
         "RZ": "circuit.add_RZ_gate({a}, {p})",
         "P": "circuit.add_U1_gate({a}, {p})",
         "CNOT": "circuit.add_CNOT_gate({c}, {t})",
         "CZ": "circuit.add_CZ_gate({a}, {b})",
         "SWAP": "circuit.add_SWAP_gate({a}, {b})",
         "CCX": "circuit.add_Toffoli_gate({c1}, {c2}, {t})"},
        measure=None),
}


def _render_gate(op, spec, qref, comment, warnings, lang):
    name = op.get("name", "")
    targets = [int(t[1:]) for t in op.get("targets", [])]
    controls = [int(c[1:]) for c in op.get("controls", [])]
    params = [p.get("expression", "0") for p in op.get("parameters", [])]
    if name == "CNOT" and len(controls) == 2:
        name = "CCX"
    if name == "CNOT" and len(controls) == 1 and targets:
        pass
    template = spec["gates"].get(name)
    if template is None:
        warnings.append("%s: no representation for gate %s, emitted as a comment"
                        % (lang, name))
        involved = ", ".join(str(x) for x in controls + targets)
        return ["%s unsupported %s gate (qubits: %s)" % (comment, name, involved)]
    if name == "CNOT":
        if len(controls) != 1 or not targets:
            warnings.append("%s: CNOT with %d controls is not supported"
                            % (lang, len(controls)))
            return ["%s unsupported CNOT arity" % comment]
        return [template.format(c=qref(controls[0]), t=qref(targets[0]))]
    if name == "CCX":
        if len(controls) != 2 or not targets:
            warnings.append("%s: CCX with %d controls is not supported"
                            % (lang, len(controls)))
            return ["%s unsupported CCX arity" % comment]
        return [template.format(c1=qref(controls[0]), c2=qref(controls[1]),
                                t=qref(targets[0]))]
    if name in TWO_QUBIT:
        if len(targets) < 2:
            return ["%s malformed %s" % (comment, name)]
        return [template.format(a=qref(targets[0]), b=qref(targets[1]))]
    if name in PARAM_ONE_QUBIT:
        return [template.format(a=qref(targets[0]), p=params[0] if params else "0")]
    if not targets:
        return ["%s malformed %s" % (comment, name)]
    return [template.format(a=qref(targets[0]))]


def emit(ir, lang):
    """Render *ir* as source code for *lang*; returns {"code", "warnings"}."""
    lang = lang.lower()
    if lang not in LANGS:
        raise ValueError("unsupported target language: %s" % lang)
    spec = LANGS[lang]
    program = ir.get("program", {})
    operations = program.get("operations", [])
    nqubits = max(len(program.get("qubits") or []) or 1, 1)
    nbits = max(len(program.get("classical_bits") or []), nqubits)
    qref, cref = spec["qref"], spec["cref"]
    comment = spec["comment"]

    warnings = []
    body = []
    measured = []
    for op in operations:
        if op.get("type") == "measure":
            qubits = [int(q[1:]) for q in op.get("qubits", [])]
            measured.extend(qubits)
            if spec["measure_style"] == "qsharp":
                continue
            template = spec["measure"]
            if template is None:
                note = ("%s: measurements are not part of the circuit model and "
                        "happen at execution time" % lang)
                if note not in warnings:
                    warnings.append(note)
                body.append("%s measurement is performed by the runtime" % comment)
                continue
            for qubit in qubits:
                body.append(template.format(a=qref(qubit), k=cref(qubit)))
            continue
        body.extend(_render_gate(op, spec, qref, comment, warnings, lang))

    if spec["measure_style"] == "qsharp":
        if not measured:
            measured = list(range(nqubits))
        elif len(set(measured)) != nqubits:
            warnings.append("qsharp: only %d of %d qubits were measured in the "
                            "source; all qubits are measured so they can be released"
                            % (len(set(measured)), nqubits))
            measured = list(range(nqubits))
        body.append("let results = [%s];"
                    % ", ".join("M(%s)" % qref(q) for q in measured))

    body = [spec["indent"] + line if line else line for line in body]
    lines = list(spec["header"](nqubits, nbits)) + body + list(spec["footer"])
    return {"code": "\n".join(lines) + "\n", "warnings": warnings}


# --------------------------------------------------------------------------
# Public API
# --------------------------------------------------------------------------

def translate(source, from_lang, to_lang, name="circuit"):
    """Translate *source* text between two supported languages."""
    from_lang = from_lang.lower()
    to_lang = to_lang.lower()
    ir, warnings = parse(source, from_lang, name=name or ("from_%s" % from_lang))
    result = emit(ir, to_lang)
    out = dict(result)
    out["ir"] = ir
    out["warnings"] = warnings + result["warnings"]
    return out


def ir_to_json(ir):
    return json.dumps(ir, indent=2)
