import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pytest
from PySide6.QtWidgets import QApplication

from resume_builder.base_editor import BaseResumeEditor, ListEditor
from resume_builder.models import BaseResume


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def complete_base():
    return BaseResume(
        name="Verified career",
        is_example=False,
        verified=True,
        identity={"name": "Alex", "email": "a@example.test", "phone": "123",
                  "location": "Remote", "links": ["https://example.test", "https://example.test/profile"]},
        profile="First line\nSecond line",
        skills=["Python", "Research\nand analysis"],
        roles=[{"id": "role-one", "employer": "Example", "title": "Engineer", "dates": "2020–2024",
                "background": "Context\ncontinued", "achievements": [
                    {"id": "evidence-one", "text": "Shipped a tool", "supporting_details": "Audit record\nA1", "critical": True},
                    {"id": "evidence-two", "text": "Supported a team", "supporting_details": "", "critical": False},
                ]},
               {"id": "role-two", "employer": "Other", "title": "Analyst", "achievements": []}],
        education=[{"qualification": "Degree", "institution": "College", "dates": "2019", "details": "Distinction"}],
        certifications=["Certificate one", "Certificate two"],
        references="Available on request\nDo not change",
        additional_sections=[{"heading": "Volunteering", "entries": ["Mentor", "Community work"]}],
    ).model_dump()


def test_full_schema_roundtrip_and_no_load_dirty_signal(app):
    editor = BaseResumeEditor()
    changes = []
    editor.dirty.connect(lambda: changes.append(True))
    original = complete_base()
    editor.set_data(original)
    assert editor.validated_data() == original
    assert not changes
    editor.fields["profile"].setPlainText("Updated profile")
    assert changes
    assert editor.data()["profile"] == "Updated profile"


def test_ordered_records_keep_all_evidence_and_stable_ids(app):
    editor = BaseResumeEditor()
    editor.set_data(complete_base())
    roles = editor.fields["roles"]
    assert isinstance(roles, ListEditor)
    roles.list.setCurrentRow(0)
    roles.move(1)
    result = editor.validated_data()
    assert [r["id"] for r in result["roles"]] == ["role-two", "role-one"]
    assert result["roles"][1]["achievements"][0]["supporting_details"] == "Audit record\nA1"
    result["roles"][1]["id"] = "external mutation"
    assert roles.data()[1]["id"] == "role-one"
    roles.remove()
    assert len(roles.data()) == 1


def test_strict_validation_and_new_ids(app):
    editor = BaseResumeEditor()
    editor.set_data(complete_base())
    editor.fields["identity"].fields["name"].clear()
    with pytest.raises(ValueError):
        editor.validated_data()
    from resume_builder.base_editor import RecordForm
    from resume_builder.models import Role
    first, second = RecordForm(Role), RecordForm(Role)
    assert first.data()["id"]
    assert first.data()["id"] != second.data()["id"]
