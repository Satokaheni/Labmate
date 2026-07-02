"""
Unit tests for catalog_prompt with mode parameter.
Tests the three rendering modes: full, terse, names.
"""

import pytest

from eval.run_routing_eval import catalog_prompt


class TestCatalogPrompt:
    """Tests for catalog_prompt with different modes."""

    @pytest.fixture
    def sample_catalog(self):
        """A small test catalog with various description patterns."""
        return {
            "code-review": "Review code for bugs. Also checks style.",
            "web-search": "Search the web",
            "ast-repo-map": "Build an AST map of the entire repository. Useful for refactoring.",
        }

    def test_full_mode_unchanged(self, sample_catalog):
        """full mode returns the full description for each skill."""
        result = catalog_prompt(sample_catalog, mode="full")
        lines = result.split("\n")

        # Should have 3 lines, one per skill
        assert len(lines) == 3

        # Check that full descriptions are present
        assert "- code-review: Review code for bugs. Also checks style." in result
        assert "- web-search: Search the web" in result
        assert (
            "- ast-repo-map: Build an AST map of the entire repository. Useful for refactoring."
            in result
        )

    def test_terse_mode_first_sentence(self, sample_catalog):
        """terse mode extracts the first sentence (up to first period)."""
        result = catalog_prompt(sample_catalog, mode="terse")
        lines = result.split("\n")

        # Should have 3 lines
        assert len(lines) == 3

        # code-review: should end at first period (first sentence is "Review code for bugs.")
        assert "- code-review: Review code for bugs." in result
        # Should NOT include "Also checks style"
        assert "Also checks style" not in result

        # web-search: no period, so use first 12 words
        assert "- web-search: Search the web" in result

        # ast-repo-map: should end at first period
        assert "- ast-repo-map: Build an AST map of the entire repository." in result
        # Should NOT include "Useful for refactoring"
        assert "Useful for refactoring" not in result

    def test_terse_mode_no_period_uses_first_12_words(self):
        """terse mode uses first 12 words when there's no period."""
        catalog = {
            "long-skill": "one two three four five six seven eight nine ten eleven twelve thirteen fourteen"
        }
        result = catalog_prompt(catalog, mode="terse")

        # Should contain only the first 12 words, not the 13th/14th
        assert (
            "- long-skill: one two three four five six seven eight nine ten eleven twelve" in result
        )
        assert "thirteen" not in result
        assert "fourteen" not in result

    def test_names_mode_only_names(self, sample_catalog):
        """names mode returns only the skill names, no descriptions."""
        result = catalog_prompt(sample_catalog, mode="names")
        lines = result.split("\n")

        # Should have 3 lines
        assert len(lines) == 3

        # Check for exact format
        assert "- code-review" in result
        assert "- web-search" in result
        assert "- ast-repo-map" in result

        # Should NOT contain any descriptions
        assert "Review code for bugs" not in result
        assert "Search the web" not in result
        assert "Build an AST map" not in result

    def test_terse_mode_strips_trailing_whitespace(self):
        """terse mode strips trailing whitespace from first sentence."""
        catalog = {"skill": "First sentence.  Second sentence."}
        result = catalog_prompt(catalog, mode="terse")

        # Should end with exactly one period, no extra whitespace
        assert result.endswith("First sentence.")

    def test_default_mode_is_full(self, sample_catalog):
        """Calling without mode parameter defaults to full."""
        full_result = catalog_prompt(sample_catalog, mode="full")
        default_result = catalog_prompt(sample_catalog)

        assert full_result == default_result

    def test_terse_mode_handles_multiple_periods(self):
        """terse mode splits on first period regardless of sentence structure."""
        catalog = {"skill": "First. Second. Third."}
        result = catalog_prompt(catalog, mode="terse")

        # Should only include up to the first period
        assert result == "- skill: First."

    def test_terse_mode_with_period_space(self):
        """terse mode correctly splits on '. ' pattern."""
        catalog = {"skill": "First sentence. Another sentence here."}
        result = catalog_prompt(catalog, mode="terse")

        assert result == "- skill: First sentence."
        assert "Another" not in result
