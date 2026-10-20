import pytest
from pydantic import ValidationError

from eidolon.config import AppConfig, IdentityConfig


NAME_FIELDS = ("user_name", "companion_name")


@pytest.mark.parametrize("values,expected", [
    ({}, {"user_name": "You", "companion_name": "Companion"}),
    ({"user_name": "Aster"}, {"user_name": "Aster", "companion_name": "Companion"}),
    ({"companion_name": "Nova"}, {"user_name": "You", "companion_name": "Nova"}),
])
def test_missing_identity_fields_receive_neutral_defaults(values, expected):
    assert IdentityConfig.model_validate(values).model_dump() == expected
    assert AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity=values).identity.model_dump() == expected
    assert AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}).identity.model_dump() == {
        "user_name": "You", "companion_name": "Companion",
    }


@pytest.mark.parametrize("field", NAME_FIELDS)
@pytest.mark.parametrize("value", [None, 42, 1.5, True, False, [], {}, b"Aster"])
def test_identity_names_accept_only_strings(field, value):
    with pytest.raises(ValidationError, match=field) as error:
        AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity={field: value})
    assert "valid string" in str(error.value)
    assert "input_value" not in str(error.value)


@pytest.mark.parametrize("value", [None, False, 42, [], "private-fixture"])
def test_explicit_invalid_identity_sections_do_not_restore_defaults(value):
    with pytest.raises(ValidationError, match="identity") as error:
        AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity=value)
    assert "private-fixture" not in str(error.value)


@pytest.mark.parametrize("field", NAME_FIELDS)
@pytest.mark.parametrize("value", [
    "Aster", "Orion Vega", "Étoile d’Or", "星の友", "رفيق", "e\u0301",
    "👩🏽‍🚀", "[bold]Nova[/bold]", "{{name}} ${HOME} ../profile",
    "N" * 80, "星" * 80, "e\u0301" * 40,
])
def test_valid_names_trim_surrounding_spaces_and_preserve_literal_unicode(field, value):
    identity = IdentityConfig(**{field: f" \u00a0{value}\u2003 "})
    assert getattr(identity, field) == value
    assert IdentityConfig.model_validate_json(identity.model_dump_json()) == identity


@pytest.mark.parametrize("field", NAME_FIELDS)
@pytest.mark.parametrize("value", ["", "   ", "\u00a0\u2003", "N" * 81, "星" * 81,
                                       "e\u0301" * 41])
def test_names_require_one_to_eighty_unicode_code_points_after_trimming(field, value):
    with pytest.raises(ValidationError, match="1.*80") as error:
        IdentityConfig(**{field: value})
    assert field in str(error.value)
    assert "input_value" not in str(error.value)


@pytest.mark.parametrize("field", NAME_FIELDS)
@pytest.mark.parametrize("code_point", [
    *range(0x20), *range(0x7F, 0xA0), 0x2028, 0x2029,
    *range(0x202A, 0x202F), *range(0x2066, 0x206A),
])
def test_forbidden_characters_are_rejected_even_at_trimmed_edges(field, code_point):
    character = chr(code_point)
    for value in (character + "Aster", "As" + character + "ter", "Aster" + character):
        with pytest.raises(ValidationError) as error:
            AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}, identity={field: value})
        message = str(error.value)
        assert f"identity.{field}" in message
        assert "control" in message or "line" in message
        assert "Aster" not in message and "input_value" not in message


@pytest.mark.parametrize("field", NAME_FIELDS)
@pytest.mark.parametrize("surrogate", ["\ud800", "\udfff", "\ud83d\ude00"])
def test_names_require_valid_utf8_without_echoing_invalid_input(field, surrogate):
    with pytest.raises(ValidationError) as error:
        IdentityConfig(**{field: "Aster" + surrogate})
    assert field in str(error.value)
    assert "UTF-8" in str(error.value)
    assert "Aster" not in str(error.value)


@pytest.mark.parametrize("field", NAME_FIELDS)
def test_assignment_uses_the_same_validation_and_preserves_the_previous_name(field):
    identity = IdentityConfig()
    setattr(identity, field, "  Shared  ")
    with pytest.raises(ValidationError):
        setattr(identity, field, "private-fixture\n")
    assert getattr(identity, field) == "Shared"


def test_roles_can_share_a_name_and_default_identity_instances_are_independent():
    identity = IdentityConfig(user_name="Shared", companion_name="Shared")
    assert identity.user_name == identity.companion_name == "Shared"
    first, second = AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"}), AppConfig(memory={"enabled": True, "save_enabled": True, "bank": "hermes"})
    first.identity.user_name = "Aster"
    assert second.identity.user_name == "You"
