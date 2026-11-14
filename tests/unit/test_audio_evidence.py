"""Audio claims must not be invented from subtitles or the movie's original language."""

import importlib
import importlib.util
from pathlib import Path

import pytest

from homeserver_control.worker.release_quality import ReleasePolicy


def _rank(title, *, languages=(), evidence=None, inferred=False, original="en"):
    release = {
        "title": title,
        "languages": languages,
        "audioLanguagesInferred": inferred,
        "seeders": 10,
        "size": 3_000_000_000,
        "quality": {"quality": {
            "resolution": 1080, "source": "bluray", "modifier": "none",
        }},
    }
    if evidence is not None:
        release["audioEvidence"] = evidence
    return ReleasePolicy(profile_name="Dublado").rank(release, original_language=original)


@pytest.mark.parametrize("title", [
    "Fixture 1080p BluRay DUAL AUDIO PTBR ENG SUBS",
    "Fixture 1080p BluRay DUAL AUDIO [PTBR+ENG]",
    "Fixture 1080p BluRay [DUAL AUDIO PTBR ENG] [SUBS ESP]",
    "Fixture 1080p BluRay [SUBS ESP] [DUAL AUDIO PTBR ENG]",
    "Fixture 1080p BluRay DUAL AUDIO [PortugueseBR+English]",
    "Fixture 1080p BluRay Audio: Brazilian Portuguese + English",
])
def test_explicit_audio_list_survives_unrelated_subtitle_markers(title):
    assert _rank(title)[0] == 3


@pytest.mark.parametrize("title", [
    "Fixture 1080p BluRay PTBR Subtitles",
    "Fixture 1080p BluRay [PTBR SUBS]",
    "Fixture 1080p BluRay SUBS PTBR ENG",
    "Fixture 1080p BluRay [SUBS PTBR] DUAL AUDIO ENG JPN",
    "Fixture 1080p BluRay DUAL AUDIO ENG JPN [PTBR SUBS]",
])
def test_subtitle_language_cannot_promote_original_release_to_dubbed(title):
    rank = _rank(title)
    assert rank is None or rank[0] == 1


def test_original_inferred_by_arr_is_not_audio_evidence():
    assert _rank("Fixture 1080p BluRay", languages=["English"], inferred=True) is None


def test_inferred_original_does_not_invent_second_track_for_ptbr_title():
    assert _rank(
        "Fixture 1080p BluRay PTBR", languages=["English"], inferred=True,
    ) is None


def test_explicit_audio_evidence_replaces_inferred_arr_languages():
    assert _rank(
        "Fixture 1080p BluRay", languages=["English"],
        evidence=[{"source": "description", "languages": ["pt-BR"]}],
    ) is None


def test_unknown_explicit_evidence_does_not_fall_back_to_arr_original():
    assert _rank(
        "Fixture 1080p BluRay", languages=["English"], evidence=[],
    ) is None


def test_description_dual_must_include_actual_original_language():
    evidence = [{"source": "description", "languages": ["pt-BR", "en"]}]
    assert _rank("Fixture", evidence=evidence, original="ja") is None


def test_confirmed_european_portuguese_blocks_title_brazilian_guess():
    evidence = [{"source": "arr-parser", "languages": ["pt-PT", "en"]}]
    assert _rank("Fixture DUAL AUDIO PTBR ENG", evidence=evidence)[0] == 1


def _description_languages(text):
    module_name = "homeserver_control.worker.audio_evidence"
    assert importlib.util.find_spec(module_name), "qualified audio evidence parser is missing"
    return importlib.import_module(module_name).description_audio_languages(text)


def test_html_description_separates_audio_from_subtitles():
    assert _description_languages(
        "<h2>Audio</h2><p>PortugueseBR + English (AC3)</p>"
        "<h2>Subtitles</h2><p>Portuguese (Portugal), Japanese</p>"
    ) == {"pt-br", "en"}


def test_subtitles_and_synopsis_never_establish_audio_tracks():
    assert _description_languages(
        "<p>A Brazilian boy learns English.</p>"
        "<p>Subtitles: Portuguese (Brazil), English</p>"
        "<script>Audio: PTBR ENG</script><style>Audio: PTBR ENG</style>"
    ) == set()


def test_mediainfo_language_in_text_track_is_not_an_audio_track():
    assert _description_languages(
        "General\nTitle : Fixture\nAudio #1\nLanguage : English\n"
        "Audio #2\nLanguage : Portuguese\nTitle : Brazilian dub\n"
        "Text #1\nLanguage : Japanese\nText #2\nLanguage : Portuguese (Portugal)\n"
    ) == {"en", "pt-br"}


def test_commentary_track_is_not_an_original_language_soundtrack():
    assert _description_languages(
        "Audio #1\nLanguage : Portuguese (Brazil)\nTitle : Dub\n"
        "Audio #2\nLanguage : English\nTitle : Director commentary\n"
        "Text #1\nLanguage : English\n"
    ) == {"pt-br"}


def test_generic_portuguese_is_not_promoted_to_brazilian():
    assert _description_languages("Áudio: Português / Inglês\nLegendas: PTBR") == {"pt", "en"}


def test_language_tags_outside_an_audio_section_are_ignored():
    assert _description_languages(
        "Language : English\nCountry: Brazil\nText\nLanguage: PTBR",
    ) == set()


def test_description_numeric_ids_unknown_and_malformed_input_are_ignored():
    assert _description_languages(None) == set()
    assert _description_languages("Audio\nLanguage: Unknown\nID: 30") == set()


@pytest.mark.parametrize(("filename", "expected"), [
    ("1337x-rav1ne-description.html", {"en", "fr", "es"}),
    ("uindex-alienvolt-description.html", {"pt", "en"}),
])
def test_real_description_layouts_read_audio_without_inventing_brazilian_tracks(filename, expected):
    description = Path(__file__).parents[1] / "fixtures" / "upstream" / filename
    assert _description_languages(description.read_text(encoding="utf-8")) == expected


def test_codec_description_with_explicit_brazilian_audio_promotes_release_to_dual():
    description = (
        '<div id="description"><strong>Subtitle(s):</strong> Portuguese [Brazilian]<br>'
        '<strong>Audio:</strong><br>'
        'Opus 7.1 - 384kbit/s - 🇬🇧 English<br>'
        'Opus 2.0 - 128kbit/s - 🇧🇷 Portuguese [Brazilian]<br>'
        '<strong>Source:</strong> BluRay</div>'
    )
    languages = _description_languages(description)
    assert languages == {"pt-br", "en"}
    assert _rank(
        "Fixture 1080p BluRay MULTi3",
        evidence=[{"source": "description", "languages": sorted(languages)}],
    )[0] == 3


@pytest.mark.parametrize("description", [
    "Audio #1 : English\nAudio #2 : Portuguese (Brazil)\nText #1 : Japanese",
    "Audio 1: English\nAudio 2: Portuguese [Brazilian]\nSubtitle(s): Japanese",
    "Audio #1 (English)\nAudio #2 (Portuguese (Brazil))\nText #1\nLanguage: Japanese",
])
def test_numbered_audio_summaries_keep_subtitle_languages_out(description):
    assert _description_languages(description) == {"en", "pt-br"}


@pytest.mark.parametrize("title", ["Brazilian dub", "Portuguese (Brazil)"])
@pytest.mark.parametrize("fields", [
    "Language: Portuguese\nTitle: {title}", "Title: {title}\nLanguage: Portuguese",
    "Idioma: Portuguese\nTitle: {title}", "Title: {title}\nIdioma: Portuguese",
])
def test_brazilian_audio_title_qualifies_generic_language_in_either_order(title, fields):
    assert _description_languages("Audio #1\n" + fields.format(title=title)) == {"pt-br"}


def test_codec_commentary_and_descriptive_audio_do_not_supply_original_track():
    assert _description_languages(
        "Audio:\nOpus 5.1 - 384kbit/s - Portuguese [Brazilian]\n"
        "Opus 2.0 - 42kbit/s - English - Descriptive Audio\n"
        "Opus 2.0 - 42kbit/s - English - Director commentary\n"
        "Subtitle(s): English\n",
    ) == {"pt-br"}


def test_source_and_synopsis_end_audio_summary_context():
    assert _description_languages(
        "Audio: English\nSource: BluRay\nPortuguese [Brazilian]\n"
        "Synopsis: A Brazilian encoder speaks Portuguese.\n"
        "Opus 5.1 - 384kbit/s - Portuguese [Brazilian]\n",
    ) == {"en"}


def test_prose_inside_audio_section_cannot_establish_brazilian_track():
    assert _description_languages(
        "Audio:\nBrazilian volunteers provided artwork.\n"
        "Portuguese filmmakers visited Brazil.\n"
        "Opus was used by a Brazilian encoder.\n",
    ) == set()


@pytest.mark.parametrize(("description", "expected"), [
    ("Audio:\nEnglish\nPortuguese [Brazilian] (subtitles only)", {"en"}),
    ("Audio:\nBrazilian (artwork provided by uploader)", set()),
    ("Audio\nLanguage: Portuguese\nTitle: Artwork by Brazilian encoder", {"pt"}),
])
def test_audio_metadata_cannot_turn_subtitles_or_author_prose_into_tracks(description, expected):
    assert _description_languages(description) == expected


def test_localized_language_field_in_text_track_cannot_establish_audio():
    assert _description_languages(
        "Audio #1\nIdioma: English\nText #1\nIdioma: Portuguese (Brazil)",
    ) == {"en"}


@pytest.mark.parametrize("header", ["Subtitles (SRT):", "Subs:"])
def test_subtitle_summary_headers_end_audio_context(header):
    assert _description_languages(
        "Audio: English\n" + header + "\nPortuguese [Brazilian]",
    ) == {"en"}


def test_full_html_page_without_known_description_does_not_read_comments():
    assert _description_languages(
        '<html><body><div id="comments">Audio: Portuguese (Brazil) + English</div>'
        '<div class="related">Audio: PTBR</div></body></html>',
    ) == set()


def test_doctype_page_with_implicit_html_body_does_not_read_comments():
    assert _description_languages(
        '<!doctype html><div id="comments">Audio: PTBR + English</div>',
    ) == set()


def test_large_html_page_only_limits_extracted_description():
    assert _description_languages(
        '<html><body><div id="comments">' + "x" * 600_000 + '</div>'
        '<div id="descrBody">Idioma: Português | Inglês</div></body></html>',
    ) == {"pt", "en"}


def test_plain_description_remains_size_bounded():
    assert _description_languages("Audio: PTBR\n" + "x" * 600_000) == set()


@pytest.mark.parametrize(("title", "original", "expected"), [
    ("Fixture 1080p BluRay Dual-Audio Brazilian Original", "ja", {"pt-br", "ja"}),
    ("Fixture 1080p BluRay Dual-Audio Brazilian Original", {"name": "Latvian"}, {"pt-br", "lv"}),
    ("Fixture 1080p BluRay Dual-Audio Brazilian Original", None, {"pt-br"}),
    ("Fixture 1080p BluRay Dual Audio", "en", {"pt-br", "en"}),
    ("Fixture 1080p BluRay Dual Áudio", "ja", {"pt-br", "ja"}),
    ("Fixture 1080p BluRay DUAL", "en", {"pt-br", "en"}),
    ("Fixture.2012.1080p.WEB-DL.DUAL-FOXX", "en", {"pt-br", "en"}),
    ("Fixture [DUAL]", "en", {"pt-br", "en"}),
    ("Fixture 2012 DUAL", "en", {"pt-br", "en"}),
    ("Fixture 720p DUAL", "en", {"pt-br", "en"}),
    ("Fixture BluRay DUAL", "en", {"pt-br", "en"}),
    ("Fixture WEB-DL DUAL", "en", {"pt-br", "en"}),
    ("Fixture BDRip DUAL", "en", {"pt-br", "en"}),
    ("Fixture BRRip DUAL", "en", {"pt-br", "en"}),
    ("Fixture S01E02 DUAL", "en", {"pt-br", "en"}),
    ("Dual Audio Fixture", "en", {"pt-br", "en"}),
    ("Fixture 1080p BluRay Dual-Audio", None, {"pt-br"}),
    ("Fixture 1080p BluRay Dublado Brazilian", "en", {"pt-br"}),
])
def test_asc_native_title_claims_are_qualified_without_inferred_arr_language(
    title, original, expected,
):
    from homeserver_control.worker.audio_evidence import release_audio_languages

    release = {
        "indexer": "Amigos Share Club", "title": title,
        "languages": ["English"], "audioEvidence": [],
    }
    assert release_audio_languages(release, original_language=original) == expected


def test_asc_raw_dual_label_is_dual_for_profile_audio_selection():
    from homeserver_control.worker.release_quality import profile_audio_tier

    release = {"indexer": "ASC", "title": "Fixture.1080p.BluRay.DUAL-GROUP"}
    assert profile_audio_tier(release, original_language="en") == "dual"


@pytest.mark.parametrize("title", [
    "Brazilian 1080p BluRay", "Fixture 1080p BluRay MULTI",
    "Fixture 1080p BluRay Original", "Duality 1080p BluRay",
    "Dual 2022 1080p WEB-DL", "The Dual 2022 1080p WEB-DL",
    "Fixture.Dual.2022.1080p.WEB-DL", "Fixture DUAL",
    "Fixture 1080p BluRay [SUBS Dublado Brazilian]",
    "Fixture 1080p BluRay Dublado Brazilian SUBS",
    "Fixture 1080p BluRay [SUBS Dual-Audio Brazilian Original]",
    "Fixture 1080p BluRay Dual-Audio Brazilian Original Subtitles",
    "Fixture 1080p BluRay [SUBS DUAL]",
    "Fixture 1080p BluRay Dual Áudio Subtitles",
])
def test_asc_bare_audio_and_subtitle_labels_do_not_invent_soundtracks(title):
    from homeserver_control.worker.audio_evidence import release_audio_languages

    release = {"indexer": "ASC", "title": title, "audioEvidence": []}
    assert release_audio_languages(release, original_language="en") == set()


@pytest.mark.parametrize("indexer", ["UIndex", "1337x", "Unknown", "Fake Amigos Share Club"])
@pytest.mark.parametrize("title", [
    "Fixture 1080p BluRay Dual-Audio Brazilian Original",
    "Fixture 1080p BluRay DUAL", "Fixture 1080p BluRay Dual Áudio",
])
def test_asc_native_words_from_unrelated_source_do_not_qualify_audio(indexer, title):
    from homeserver_control.worker.audio_evidence import release_audio_languages

    release = {
        "indexer": indexer,
        "title": title,
        "audioEvidence": [],
    }
    assert release_audio_languages(release, original_language="en") == set()


@pytest.mark.parametrize(("source", "languages", "expected"), [
    ("arr-parser", ["pt-PT", "en"], {"pt-pt", "en", "ja"}),
    ("description", ["pt-PT"], {"pt-pt", "ja"}),
    ("ffprobe", [], set()),
    ("ffprobe", ["en"], {"en"}),
    ("ffprobe", ["pt-BR"], {"pt-br"}),
    ("ffprobe", ["pt-PT", "en"], {"pt-pt", "en"}),
])
@pytest.mark.parametrize("title", [
    "Fixture 1080p BluRay Dual-Audio Brazilian Original",
    "Fixture 1080p BluRay DUAL",
])
def test_asc_native_claims_preserve_conflicting_or_observed_audio(
    source, languages, expected, title,
):
    from homeserver_control.worker.audio_evidence import release_audio_languages

    release = {
        "indexer": "Amigos Share Club",
        "title": title,
        "audioEvidence": [{"source": source, "languages": languages}],
    }
    assert release_audio_languages(release, original_language="ja") == expected
