from services.orchestrator.sandbox_edits import detect_sandbox_writes


def _ok_env():
    return {"ok": True, "result": {"content": [{"type": "text",
            "text": '{"stdout": "", "stderr": "", "exit_code": 0}'}], "isError": False}}


def _fail_env():
    return {"ok": True, "result": {"content": [{"type": "text",
            "text": '{"exit_code": 1}'}], "isError": True}}


def test_heredoc_redirect_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "cat <<'EOF' > /workspace/ab_factorial.py\n...\nEOF"},
        _ok_env(),
    )
    assert "/workspace/ab_factorial.py" in paths


def test_simple_redirect_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "echo hi > notes.txt"}, _ok_env(),
    )
    assert "notes.txt" in paths


def test_tee_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "echo x | tee out/result.log"}, _ok_env(),
    )
    assert "out/result.log" in paths


def test_python_open_write_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_python",
        {"code": "open('/workspace/test_f.py', 'w').write('...')"}, _ok_env(),
    )
    assert "/workspace/test_f.py" in paths


def test_python_pathlib_write_text_detected():
    paths = detect_sandbox_writes(
        "code-sandbox", "run_python",
        {"code": "from pathlib import Path\nPath('a/b.py').write_text('x')"}, _ok_env(),
    )
    assert "a/b.py" in paths


def test_no_write_returns_empty():
    assert detect_sandbox_writes(
        "code-sandbox", "run_python", {"code": "print(2+2)"}, _ok_env()
    ) == set()


def test_failed_run_not_counted():
    assert detect_sandbox_writes(
        "code-sandbox", "run_shell", {"cmd": "echo x > f.txt"}, _fail_env()
    ) == set()


def test_other_skill_returns_empty():
    assert detect_sandbox_writes(
        "test-gen", "generate", {"cmd": "echo x > f.txt"}, _ok_env()
    ) == set()


def test_read_only_redirect_not_a_write():
    # input redirection / comparison must not be treated as a write
    assert detect_sandbox_writes(
        "code-sandbox", "run_shell", {"cmd": "cat < input.txt"}, _ok_env()
    ) == set()


def test_greater_than_comparison_not_a_write():
    # shell comparison operators (>, >=) must not be treated as writes
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "if [ $a > b ]; then echo hi; fi"}, _ok_env(),
    )
    assert paths == set(), f"Expected empty set but got {paths}"


def test_gte_comparison_not_a_write():
    # >= comparison must not be treated as a write
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "if [ $a >= b ]; then echo hi; fi"}, _ok_env(),
    )
    assert paths == set(), f"Expected empty set but got {paths}"


def test_python_gte_comparison_not_a_write():
    # Python >= comparison must not be treated as a write
    paths = detect_sandbox_writes(
        "code-sandbox", "run_python",
        {"code": "x = 1 if a >= b else 2"}, _ok_env(),
    )
    assert paths == set(), f"Expected empty set but got {paths}"


def test_process_substitution_not_a_write():
    # process substitution >(cat) must not be treated as a write
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "tee >(cat)"}, _ok_env(),
    )
    assert paths == set(), f"Expected empty set but got {paths}"


def test_double_bracket_comparison_multichar_not_a_write():
    # [[ $count > threshold ]] must not be treated as write to 'threshold'
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "if [[ $count > threshold ]]; then echo hi; fi"}, _ok_env(),
    )
    assert paths == set(), f"Expected empty set but got {paths}"


def test_single_bracket_comparison_multichar_not_a_write():
    # [ $a > bb ] must not be treated as write to 'bb'
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "if [ $a > bb ]; then echo hi; fi"}, _ok_env(),
    )
    assert paths == set(), f"Expected empty set but got {paths}"


def test_arithmetic_comparison_multichar_not_a_write():
    # (( a > bb )) must not be treated as write to 'bb'
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "if (( a > bb )); then echo hi; fi"}, _ok_env(),
    )
    assert paths == set(), f"Expected empty set but got {paths}"


def test_legitimate_redirect_with_multichar_name_detected():
    # A legitimate redirect to a file with a dot (extension) should still be detected
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "echo hi > output.txt"}, _ok_env(),
    )
    assert "output.txt" in paths, f"Expected output.txt in {paths}"


def test_legitimate_redirect_with_path_detected():
    # A redirect with a path separator should still be detected
    paths = detect_sandbox_writes(
        "code-sandbox", "run_shell",
        {"cmd": "echo hi > /tmp/output.txt"}, _ok_env(),
    )
    assert "/tmp/output.txt" in paths, f"Expected /tmp/output.txt in {paths}"
