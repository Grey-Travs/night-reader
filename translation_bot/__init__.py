"""Korean web-novel translation pipeline.

Reads a Google Doc (one chapter per tab), translates Korean prose into native
English web-novel prose with Claude, and writes one clean Markdown file per
chapter. Fidelity first, then name/term consistency, then style.
"""

__version__ = "0.1.0"
