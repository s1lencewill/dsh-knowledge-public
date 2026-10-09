"""One engine process per query. Untrusted side of the pipeline: fixtures only in M0.

This process never receives a path into the library. It is handed the snapshot directory the
trusted parent prepared (`snapshotDir`), runs the engine over that copy, and returns *candidates*.
Verification against the original files happens in engine/pipeline.py, in the parent.
"""
import asyncio
import json
import os
import sys
from pathlib import Path
from runtime import prepare_environment, model_settings, safe_error
import sirchmunk_compat as compat
from sirchmunk_compat import close_engine, install_rga_cache_path, configure_retrieval


def candidates_from(context):
    """Engine evidence units, reduced to the untrusted claim: a file path and raw snippet values."""
    cluster = (context.get("cluster") or {}) if isinstance(context, dict) else {}
    units = []
    for unit in cluster.get("evidences", cluster.get("evidence_units", [])) or []:
        if not isinstance(unit, dict):
            continue
        snippets = unit.get("snippets") or []
        if isinstance(snippets, str):
            snippets = [snippets]
        units.append({"file": unit.get("file_or_url") or "",
                      "is_found": unit.get("is_found"),
                      "snippets": list(snippets)})
    return units


def candidate_warnings(candidates, context):
    """Name *why* a result carries the citations it does.

    Recovering the evidence the engine read but declined to cache is not the same claim as the engine
    having accepted it, and neither is the same as nothing having matched. Keeping those three apart is
    what lets the parent's result be read honestly instead of collapsing into "no candidates".
    """
    facts = context.get(compat.RECOVERY_KEY) if isinstance(context, dict) else None
    if not candidates:
        if isinstance(facts, dict):
            return ["ENGINE_READ_FILES_WITHOUT_EVIDENCE: %d file(s) read, no usable evidence"
                    % facts.get("filesRead", 0), "NO_ENGINE_CANDIDATES"]
        return ["NO_ENGINE_CANDIDATES"]
    if isinstance(facts, dict) and facts.get("engineAccepted") is False:
        return ["ENGINE_REJECTED_EVIDENCE: %d file(s) were read but the engine did not accept the "
                "evidence; the excerpts are parent-side windows, not the engine's answer"
                % facts.get("filesRead", 0)]
    if isinstance(facts, dict):
        return ["ENGINE_CACHE_SKIPPED_BY_MODEL: the engine read %d file(s) and declined to cache the "
                "cluster; the excerpts are what it actually read" % facts.get("filesRead", 0)]
    return []


async def run(request):
    # Only this short-lived worker is changed, never the parent process or global environment.
    os.environ.update(prepare_environment(os.environ))
    mode = request["mode"]
    # With a model pipe the engine has no key and no URL: every call goes through the parent proxy.
    model_pipe = request.get("modelPipe")
    settings = model_settings(mode, os.environ, via_proxy=bool(model_pipe))
    if model_pipe:
        from llm_bridge import install_bridge
        # The parent states what is left of the job budget; the bridge bounds every call and retry by
        # it, so a lost reply cannot extend the job.
        install_bridge(model_pipe, request.get("modelName"), budget_seconds=request.get("modelBudgetSeconds"))
    snapshot_dir = request.get("snapshotDir")
    if not snapshot_dir or not Path(snapshot_dir).is_dir():
        raise RuntimeError("SNAPSHOT_MISSING: the parent must hand over a prepared snapshot")
    state_dir = Path(request.get("stateDir") or (Path(request["workDir"]) / "state"))
    state_dir.mkdir(parents=True, exist_ok=True)
    # rga resolves its own cache through the Windows known-folder API, which fails inside an
    # AppContainer; give every rga call a cache directory this process is allowed to write.
    install_rga_cache_path(state_dir / "rga-cache")

    from sirchmunk import AgenticSearch
    from sirchmunk.llm import OpenAIChat
    from runtime_min import CAPABILITIES
    # The engine's optional machinery is configured *from* the runtime's declaration, never from
    # literals that could drift: the minimal image has no torch/modelscope, so knowledge reuse, its
    # evolution or embeddings must be off. Tests and the startup gate keep the declaration honest.
    incompatible = [name for name, enabled in (("embeddings", CAPABILITIES.get("embeddings")),
                                               ("knowledgeReuse", CAPABILITIES.get("knowledgeReuse")),
                                               ("knowledgeEvolution", CAPABILITIES.get("knowledgeEvolution")))
                    if enabled]
    if incompatible:
        raise RuntimeError("RUNTIME_INCOMPATIBLE: the engine cannot honour %s" % ", ".join(incompatible))
    llm, searcher = None, None
    try:
        llm = OpenAIChat(**settings)
        searcher = AgenticSearch(llm=llm, work_path=state_dir,
                                 reuse_knowledge=bool(CAPABILITIES.get("knowledgeReuse")),
                                 enable_knowledge_evolution=bool(CAPABILITIES.get("knowledgeEvolution")),
                                 verbose=False)
        configure_retrieval(searcher)
        context = await searcher.search(request["query"], paths=[str(snapshot_dir)], mode=mode,
                                        response_format="json", max_loops=4, top_k_files=10,
                                        max_token_budget=16000, enable_dir_scan=False, llm_fallback=False)
    finally:
        await close_engine(searcher, llm)
    if mode == "FILENAME_ONLY":
        return {"status": "partial", "answer": "", "candidates": [], "evidence": [],
                "warnings": ["Filename-only engine probe; no intelligent search or citation validation"],
                "matches": [{"name": Path(str(value.get('file_path', value.get('path', '')))).name}
                            for value in context]}
    # The engine's own answer is never returned: the parent builds the answer-facing result.
    candidates = candidates_from(context)
    facts = context.get(compat.RECOVERY_KEY) if isinstance(context, dict) else None
    engine_facts = None
    if isinstance(facts, dict):
        # Diagnostics only, and untrusted like everything else the worker returns: the parent re-checks
        # each reported span against the snapshot before recording it. No text travels this way.
        engine_facts = {"sampledSpans": facts.get("sampledSpans") or {},
                        "sampledUnlocatable": facts.get("sampledUnlocatable"),
                        "matchFacts": facts.get("matchFacts"),
                        "filesRead": facts.get("filesRead")}
    return {"status": "partial", "answer": "", "candidates": candidates,
            "warnings": candidate_warnings(candidates, context),
            "engineFacts": engine_facts,
            "usage": (context.get("llm_usages") or None) if isinstance(context, dict) else None}


if __name__ == "__main__":
    try:
        result = asyncio.run(run(json.loads(sys.stdin.read())))
    except Exception as error:
        # Error class only: third-party exceptions can contain credentials or source text.
        result = {"status": "failed", "answer": "", "evidence": [], "warnings": [safe_error(error)]}
    print("DSH_KNOWLEDGE_RESULT=" + json.dumps(result, ensure_ascii=True), flush=True)
