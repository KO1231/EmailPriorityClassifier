"""Turning a priority into changes to make in Gmail.

This package must not import :mod:`epc.classify`. The separation is what lets
the apply worker run without an LLM SDK or a MIME parser, and it is enforced by
a test rather than by convention.
"""
