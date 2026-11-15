"""The content language (pt-BR support, 2026-08-20)."""

from __future__ import annotations

import pytest

from videoforge_shared.language import UnknownLanguageError, resolve


class TestResolve:
    def test_brazilian_portuguese_is_named_not_tagged(self) -> None:
        """**The reason this table exists.**

        A model asked to write in "pt-BR" complies and drifts toward European
        Portuguese — different pronouns, different verb forms, a different
        register — which a Brazilian audience hears in the first sentence. The
        prompt gets the name, so there is nothing to drift toward.
        """
        language = resolve("pt-BR")

        assert language.name == "Brazilian Portuguese"
        assert language.tag == "pt-BR"

    def test_the_voice_code_drops_the_region(self) -> None:
        """ElevenLabs' ``language_code`` takes ISO-639-1. The regional variety
        is carried by the *voice* — a Brazilian voice id — not by this, and
        sending it a full BCP-47 tag is how you get a 422."""
        assert resolve("pt-BR").voice_code == "pt"
        assert resolve("es-419").voice_code == "es"
        assert resolve("en").voice_code == "en"

    def test_an_unlisted_but_valid_tag_still_works(self) -> None:
        """Refusing to run in Dutch because nobody wrote "Dutch" into a table
        would be a gratuitous limit. The tag becomes its own name, which a
        model handles perfectly well; only the register nuance is lost."""
        language = resolve("nl")

        assert language.name == "nl"
        assert language.voice_code == "nl"

    @pytest.mark.parametrize("bad", ["pt_BR", "portuguese (brazil)", "", "p", "PT-br"])
    def test_a_malformed_tag_is_refused(self, bad: str) -> None:
        """The two consumers fail very differently on a typo, which is why this
        is validated rather than trusted.

        An LLM told to write in "pt_BR" writes Portuguese anyway and nobody
        notices the setting is malformed. ElevenLabs rejects the same string
        outright. Without this check the result is a working script stage, a
        broken voice stage, and no obvious connection between them.
        """
        with pytest.raises(UnknownLanguageError, match="BCP-47"):
            resolve(bad)

    def test_surrounding_whitespace_survives_a_dotenv_file(self) -> None:
        """`CONTENT_LANGUAGE=pt-BR ` with a trailing space is an ordinary way
        to edit a `.env`, and failing on it would be pedantry."""
        assert resolve("  pt-BR  ").tag == "pt-BR"
