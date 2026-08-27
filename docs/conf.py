"""Sphinx configuration for the Quilombo documentation."""

project = "Quilombo"
copyright = "2026, Quilombo contributors"
author = "Quilombo contributors"
release = "0.4.3"

extensions = [
    "myst_parser",
    "sphinxcontrib.mermaid",
]

exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]
myst_enable_extensions = ["colon_fence", "deflist"]
myst_heading_anchors = 3

html_theme = "sphinx_book_theme"
html_title = "Quilombo documentation"
html_theme_options = {
    "repository_url": "https://github.com/mgaitan/quilombo",
    "use_repository_button": True,
    "use_issues_button": True,
}
