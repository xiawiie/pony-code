from pico import Pico
from pico.model_output_parser import parse_attrs, parse_model_output, parse_xml_tool


def test_parse_model_output_accepts_xml_tool_with_multiline_content():
    raw = '<tool name="write_file" path="hello.py"><content>print("hi")\n</content></tool>'

    kind, payload = parse_model_output(raw)

    assert kind == "tool"
    assert payload == {
        "name": "write_file",
        "args": {
            "path": "hello.py",
            "content": 'print("hi")\n',
        },
    }


def test_parse_model_output_retries_on_malformed_json_tool():
    kind, payload = parse_model_output("<tool>{bad json</tool>")

    assert kind == "retry"
    assert "malformed tool JSON" in payload


def test_parse_attrs_accepts_single_and_double_quoted_values():
    attrs = parse_attrs(""" name="write_file" path='hello.py' """)

    assert attrs == {"name": "write_file", "path": "hello.py"}


def test_pico_parse_delegates_to_model_output_parser():
    raw = "<final>Done.</final>"

    assert Pico.parse(raw) == parse_model_output(raw)
    assert Pico.parse_xml_tool('<tool name="delegate">inspect README</tool>') == parse_xml_tool(
        '<tool name="delegate">inspect README</tool>'
    )
