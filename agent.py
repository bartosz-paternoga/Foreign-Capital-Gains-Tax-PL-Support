"""Backward-compatible Streamlit entry point.

The tax application has one implementation so both supported launch commands
use the same parser, validation rules, reports, authentication, and calculations.
"""
from app import *  # noqa: F401,F403
