"""Remote management for headless Sparks: terminal, logs, support bundle, power, network boot.

Everything that can change the machine is off until the operator switches it on **on that node**
(``sudo tsm remote enable …``), which writes a root-owned policy file. The web GUI and the
controller cannot flip those switches; they can only use what the node already allows.
"""
