# /!\ Autogenerated code, modifications will be lost /!\
# see `tools/generate_builtins.py`

cimport cython
from libc.stdint cimport uintptr_t

from godot._hazmat.gdapi cimport (
    pythonscript_gdapi as gdapi,
    pythonscript_gdapi11 as gdapi11,
    pythonscript_gdapi12 as gdapi12,
)


{% set render_target = "rid" %}
{% include 'render.tmpl.pyx' with context  %}
{% set render_target = "vector3" %}
{% include 'render.tmpl.pyx' with context  %}
