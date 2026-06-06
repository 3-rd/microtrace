"""Context Compaction 测试 (SPEC §8.1)"""
import pytest
from microtrace.context.models import (
    Context, Evidence, ToolCall, ContentType,
    EvidenceSource, EvidenceImportance, ToolState,
)
from microtrace.context.compaction import (
    is_overflow,
    extract_microtrace_critical_lines,
    _prune_old_tool_outputs,
    MICROTRACE_PRESERVE_PATTERNS,
    COMPACTION_BUFFER,
    DEFAULT_TAIL_TURNS,
)
from microtrace.context.prompt import determine_content_type


# ── is_overflow ────────────────────────────────────────────────

def test_is_overflow_when_above_threshold():
    """tokens >= context_window - buffer → overflow"""
    ctx = Context(cumulative_tokens=120000)
    assert is_overflow(ctx, context_window=128000) == True


def test_is_overflow_false_when_below():
    """tokens < threshold → not overflow"""
    ctx = Context(cumulative_tokens=50000)
    assert is_overflow(ctx, context_window=128000) == False


def test_is_overflow_exact_boundary():
    """边界值：== threshold 也算 overflow（>=）"""
    ctx = Context(cumulative_tokens=108000)  # 128000 - 20000 = 108000
    assert is_overflow(ctx, context_window=128000) == True


def test_compaction_buffer_is_20k():
    """COMPACTION_BUFFER = 20_000（与 OpenCode 一致）"""
    assert COMPACTION_BUFFER == 20_000


def test_default_tail_turns_is_2():
    """DEFAULT_TAIL_TURNS = 2"""
    assert DEFAULT_TAIL_TURNS == 2


# ── extract_microtrace_critical_lines ─────────────────────────

def test_critical_lines_extracts_java_exceptions():
    """提取 Java 异常行"""
    output = (
        "INFO started\n"
        "java.lang.NullPointerException\n"
        "  at com.foo.Bar.method(Bar.java:42)\n"
        "Caused by: java.io.IOException\n"
        "DEBUG done\n"
    )
    lines = extract_microtrace_critical_lines(output)
    assert "NullPointerException" in lines
    assert "Bar.java:42" in lines
    assert "Caused by" in lines
    assert "DEBUG done" not in lines
    assert "INFO started" not in lines


def test_critical_lines_extracts_dubbo_feign():
    """提取 @FeignClient / @DubboReference"""
    output = (
        '@FeignClient(name = "user-service")\n'
        "public interface UserClient {}\n"
        "@DubboReference\n"
        "private OrderService orderService;\n"
    )
    lines = extract_microtrace_critical_lines(output)
    assert "@FeignClient" in lines
    assert "@DubboReference" in lines


def test_critical_lines_extracts_error_codes():
    """提取 HTTP 错误码 / error code"""
    output = (
        "returned status 500\n"
        "HTTP/1.1 404 Not Found\n"
        "error code: 1001\n"
    )
    lines = extract_microtrace_critical_lines(output)
    assert "returned status 500" in lines
    assert "HTTP/1.1 404" in lines
    assert "error code: 1001" in lines


def test_critical_lines_extracts_annotations():
    """提取 @Transactional / @Async / @Scheduled"""
    output = (
        "@Transactional\n"
        "public void save() {}\n"
        "@Async\n"
        "public void run() {}\n"
        "@Scheduled\n"
        "public void tick() {}\n"
    )
    lines = extract_microtrace_critical_lines(output)
    assert "@Transactional" in lines
    assert "@Async" in lines
    assert "@Scheduled" in lines


def test_critical_lines_extracts_log_levels():
    """提取 ERROR / FATAL 日志级别"""
    output = "INFO normal\nERROR something bad\nFATAL critical\n"
    lines = extract_microtrace_critical_lines(output)
    assert "ERROR something bad" in lines
    assert "FATAL critical" in lines
    assert "INFO normal" not in lines


def test_critical_lines_limits_to_20():
    """最多 20 行"""
    output = "\n".join(f"ERROR line {i}" for i in range(50))
    lines = extract_microtrace_critical_lines(output)
    assert len(lines.split("\n")) <= 20


def test_critical_lines_empty_output():
    """空输出"""
    assert extract_microtrace_critical_lines("") == ""


def test_critical_lines_patterns_count():
    """MICROTRACE_PRESERVE_PATTERNS 至少 12 种"""
    assert len(MICROTRACE_PRESERVE_PATTERNS) >= 12


# ── _prune_old_tool_outputs ──────────────────────────────────

def test_prune_skips_recent_tool_calls():
    """跳过最近 DEFAULT_TAIL_TURNS=2 轮"""
    ctx = Context()
    # 5 个 tool call 都成功
    for i in range(5):
        ctx.add_tool_call(ToolCall(
            name="find_class", args={"x": i}, args_summary=f"x={i}",
            output_summary="found", iteration=i+1, state=ToolState.COMPLETED,
        ))
        ev = Evidence(
            source=EvidenceSource.TOOL_OUTPUT, location="tool:find_class",
            content="x" * 5000, raw_content="x" * 5000,
            discovered_at_iteration=i+1, tool_name="find_class",
        )
        ctx.add_evidence(ev)

    pruned = _prune_old_tool_outputs(ctx)
    # 最近 2 轮不动（DEFAULT_TAIL_TURNS=2）
    # 前 3 轮被 prune
    assert pruned == 3
    assert ctx.evidence[0].compacted == True
    assert ctx.evidence[1].compacted == True
    assert ctx.evidence[2].compacted == True
    assert ctx.evidence[3].compacted == False
    assert ctx.evidence[4].compacted == False


def test_prune_no_op_when_too_few_calls():
    """tool call 数 <= DEFAULT_TAIL_TURNS → no-op"""
    ctx = Context()
    for i in range(2):
        ctx.add_tool_call(ToolCall(
            name="find_class", args={"x": i}, args_summary="x",
            output_summary="o", iteration=i+1, state=ToolState.COMPLETED,
        ))
    assert _prune_old_tool_outputs(ctx) == 0


def test_prune_truncates_long_content():
    """PRUNE 截断 content 到 TOOL_OUTPUT_MAX_CHARS=2000"""
    from microtrace.context.compaction import TOOL_OUTPUT_MAX_CHARS
    ctx = Context()
    long_content = "x" * 5000
    for i in range(3):
        ctx.add_tool_call(ToolCall(
            name="find_class", args={"x": i}, args_summary="x",
            output_summary="o", iteration=i+1, state=ToolState.COMPLETED,
        ))
        ctx.add_evidence(Evidence(
            source=EvidenceSource.TOOL_OUTPUT, location="tool:find_class",
            content=long_content, raw_content=long_content,
            discovered_at_iteration=i+1, tool_name="find_class",
        ))
    _prune_old_tool_outputs(ctx)
    # 第 1 条被 PRUNE，content 截断到 2000
    assert len(ctx.evidence[0].content) == TOOL_OUTPUT_MAX_CHARS


# ── determine_content_type（5 条结构规则）─────────────────────

def test_determine_rule1_stack_source():
    """规则 1: source=stack → critical"""
    ev = Evidence(
        source=EvidenceSource.STACK, location="Foo.java:42",
        content="x", raw_content="x", discovered_at_iteration=1,
    )
    ctx = Context(max_iterations=8)
    assert determine_content_type(ev, ctx) == ContentType.CRITICAL


def test_determine_rule2_code_with_annotation():
    """规则 2: source=code + 含 @ → critical"""
    ev = Evidence(
        source=EvidenceSource.CODE, location="X",
        content="@Transactional method body", raw_content="x",
        discovered_at_iteration=1,
    )
    ctx = Context(max_iterations=8)
    assert determine_content_type(ev, ctx) == ContentType.CRITICAL


def test_determine_rule2_code_without_annotation():
    """规则 2 反例: code 不含 @ 且 iter>max/2 → compressible"""
    ev = Evidence(
        source=EvidenceSource.CODE, location="X",
        content="just some code", raw_content="x",
        discovered_at_iteration=6,  # > 8/2=4，避免规则 4 触发
    )
    ctx = Context(max_iterations=8)
    assert determine_content_type(ev, ctx) == ContentType.COMPRESSIBLE


def test_determine_rule3_log_with_java_line():
    """规则 3: source=log + at X.java:N → critical"""
    ev = Evidence(
        source=EvidenceSource.LOG, location="X",
        content="ERROR at UserService.java:42", raw_content="x",
        discovered_at_iteration=1,
    )
    ctx = Context(max_iterations=8)
    assert determine_content_type(ev, ctx) == ContentType.CRITICAL


def test_determine_rule4_early_evidence():
    """规则 4: iter <= max/2 → critical"""
    ev = Evidence(
        source=EvidenceSource.TOOL_OUTPUT, location="X",
        content="y", raw_content="x",
        discovered_at_iteration=2,  # <= 8/2=4
    )
    ctx = Context(max_iterations=8)
    assert determine_content_type(ev, ctx) == ContentType.CRITICAL


def test_determine_rule4_late_evidence_compressible():
    """规则 4 反例: iter > max/2 → compressible"""
    ev = Evidence(
        source=EvidenceSource.TOOL_OUTPUT, location="X",
        content="y", raw_content="x",
        discovered_at_iteration=6,  # > 8/2=4
    )
    ctx = Context(max_iterations=8)
    assert determine_content_type(ev, ctx) == ContentType.COMPRESSIBLE


def test_determine_rule5_llm_critical():
    """规则 5: importance=critical → critical"""
    ev = Evidence(
        source=EvidenceSource.TOOL_OUTPUT, location="X",
        content="y", raw_content="x",
        importance=EvidenceImportance.CRITICAL,
        discovered_at_iteration=6,
    )
    ctx = Context(max_iterations=8)
    assert determine_content_type(ev, ctx) == ContentType.CRITICAL


def test_determine_compressible_default():
    """默认 compressible（5 条规则都不匹配）"""
    ev = Evidence(
        source=EvidenceSource.TOOL_OUTPUT, location="X",
        content="y", raw_content="x",
        discovered_at_iteration=6,
    )
    ctx = Context(max_iterations=8)
    assert determine_content_type(ev, ctx) == ContentType.COMPRESSIBLE
