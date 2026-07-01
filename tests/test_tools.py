"""4 个最小工具测试 (SPEC §8.1)"""
import os
import tempfile
import pytest
from microtrace.tools import (
    ReadFileTool, ReadFileInput,
    SearchLogsTool, SearchLogsInput,
    FindClassTool, FindClassInput,
    ParseStackTraceTool, ParseStackTraceInput,
)


# ── read_file ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_read_file_happy_path():
    """read_file 正常读取"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("line 1\nline 2\nline 3\nline 4\nline 5\n")
        path = f.name
    try:
        result = await ReadFileTool().execute({
            "file_path": path, "offset": 0, "limit": 10
        })
        assert result.success
        assert "line 1" in result.content
        assert "line 5" in result.content
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_read_file_with_offset_limit():
    """read_file 限定行范围"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("\n".join(f"line {i}" for i in range(1, 11)))
        path = f.name
    try:
        result = await ReadFileTool().execute({
            "file_path": path, "offset": 3, "limit": 2
        })
        assert result.success
        # offset=3 (0-based) = 第 4 行（line 4），limit=2 = line 4-5
        assert "line 4" in result.content
        assert "line 5" in result.content
        assert "line 6" not in result.content
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_read_file_not_found():
    """read_file 文件不存在"""
    result = await ReadFileTool().execute({"file_path": "/nonexistent/path"})
    assert not result.success
    assert "不存在" in result.error


@pytest.mark.asyncio
async def test_read_file_path_traversal():
    """read_file 拒绝 .. 穿越"""
    result = await ReadFileTool().execute({"file_path": "../../../etc/passwd"})
    assert not result.success
    assert "traversal" in result.error.lower() or "参数错误" in result.error


@pytest.mark.asyncio
async def test_read_file_too_large():
    """read_file 文件超过 max_bytes 拒绝"""
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False) as f:
        f.write("x" * 1000)
        path = f.name
    try:
        result = await ReadFileTool().execute({
            "file_path": path, "max_bytes": 100
        })
        assert not result.success
        assert "过大" in result.error
    finally:
        os.unlink(path)


@pytest.mark.asyncio
async def test_read_file_input_validation():
    """read_file 错误参数"""
    result = await ReadFileTool().execute({"offset": -1})
    assert not result.success
    assert "参数错误" in result.error


# ── search_logs ──────────────────────────────────────────────

@pytest.mark.asyncio
async def test_search_logs_happy_path():
    """search_logs 找到匹配"""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = os.path.join(tmpdir, "test.log")
        with open(log_file, "w") as f:
            f.write("2026-01-01 INFO started\n")
            f.write("2026-01-01 ERROR NullPointerException\n")
            f.write("2026-01-01 INFO done\n")
        result = await SearchLogsTool().execute({
            "keyword": "ERROR", "log_dir": tmpdir
        })
        assert result.success
        assert "NullPointerException" in result.content
        assert "找到 1 条匹配" in result.content


@pytest.mark.asyncio
async def test_search_logs_multi_keywords():
    """search_logs 多关键词（OR 关系）"""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = os.path.join(tmpdir, "test.log")
        with open(log_file, "w") as f:
            f.write("ERROR foo\nWARN bar\nINFO baz\n")
        result = await SearchLogsTool().execute({
            "keyword": "ERROR,WARN", "log_dir": tmpdir
        })
        assert result.success
        assert "foo" in result.content
        assert "bar" in result.content
        assert "baz" not in result.content


@pytest.mark.asyncio
async def test_search_logs_dir_not_found():
    """search_logs 目录不存在"""
    result = await SearchLogsTool().execute({
        "keyword": "ERROR", "log_dir": "/nonexistent/dir"
    })
    assert not result.success


@pytest.mark.asyncio
async def test_search_logs_no_match():
    """search_logs 无匹配"""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = os.path.join(tmpdir, "test.log")
        with open(log_file, "w") as f:
            f.write("INFO started\n")
        result = await SearchLogsTool().execute({
            "keyword": "nonexistent_xyz_abc", "log_dir": tmpdir
        })
        assert result.success
        assert "未找到" in result.content


# ── find_class ───────────────────────────────────────────────

@pytest.mark.asyncio
async def test_find_class_happy_path():
    """find_class 找到 Java 文件"""
    with tempfile.TemporaryDirectory() as tmpdir:
        # 创建 src/main/java/UserService.java
        java_dir = os.path.join(tmpdir, "src", "main", "java")
        os.makedirs(java_dir)
        java_file = os.path.join(java_dir, "UserService.java")
        with open(java_file, "w") as f:
            f.write("package com.foo;\n")
            f.write("public class UserService {\n")
            f.write("    public void getUser() {}\n")
            f.write("}\n")
        result = await FindClassTool().execute({
            "class_name": "UserService", "search_root": tmpdir
        })
        assert result.success
        assert "UserService" in result.content
        assert "UserService.java" in result.content


@pytest.mark.asyncio
async def test_find_class_not_found():
    """find_class 找不到"""
    with tempfile.TemporaryDirectory() as tmpdir:
        result = await FindClassTool().execute({
            "class_name": "NonExistent", "search_root": tmpdir
        })
        assert result.success
        assert "未找到" in result.content


@pytest.mark.asyncio
async def test_find_class_lowercase_rejected():
    """find_class 拒绝小写开头（Java 规范）"""
    result = await FindClassTool().execute({"class_name": "lowercase"})
    assert not result.success
    assert "大写" in result.error


# ── parse_stack_trace ────────────────────────────────────────

@pytest.mark.asyncio
async def test_parse_stack_trace_happy_path():
    """parse_stack_trace 解析标准 Java 堆栈"""
    stack = """java.lang.NullPointerException
	at com.foo.Bar.method(Bar.java:42)
	at com.foo.Main.main(Main.java:10)
	Caused by: java.io.IOException: timeout
"""
    result = await ParseStackTraceTool().execute({"stack_text": stack})
    assert result.success
    assert "Bar.java:42" in result.content
    assert "Main.java:10" in result.content
    assert "Caused by" in result.content  # critical line extracted


@pytest.mark.asyncio
async def test_parse_stack_trace_empty():
    """parse_stack_trace 空文本"""
    result = await ParseStackTraceTool().execute({"stack_text": ""})
    assert result.success
    assert "空" in result.content


@pytest.mark.asyncio
async def test_parse_stack_trace_no_frames():
    """parse_stack_trace 无法解析（无堆栈帧格式）"""
    result = await ParseStackTraceTool().execute({
        "stack_text": "Some random text without stack frames"
    })
    assert result.success
    assert "未解析到" in result.content


@pytest.mark.asyncio
async def test_parse_stack_trace_top_n():
    """parse_stack_trace top_n 限制"""
    stack = "\n".join(
        f"\tat com.foo.Class{i}.method(File{i}.java:{i})" for i in range(20)
    )
    result = await ParseStackTraceTool().execute({
        "stack_text": stack, "top_n": 3
    })
    assert result.success
    # top 3 = File0, File1, File2 出现在"解析到的堆栈帧"部分
    assert "File0.java:0" in result.content
    assert "File2.java:2" in result.content
    # 验证只有 3 个 frame 编号（1./2./3.）
    import re
    frame_lines = [l for l in result.content.split("\n") if re.match(r"^\s+\d+\.\s", l)]
    assert len(frame_lines) == 3
