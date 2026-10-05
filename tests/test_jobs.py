from fmapp.core.operations import Job, Step
from fmapp.ui.jobs import JobManager, RunState, _Output


def test_secret_split_across_reads_is_still_masked():
    sink: list[str] = []
    out = _Output(["ghp_supersecret"], sink.append)
    out.feed(b"cloning https://x-access-token:ghp_super")
    out.feed(b"secret@github.com/org/app\nnext line\n")
    out.flush()
    text = "".join(sink)
    assert "supersecret" not in text and "ghp_super" not in text
    assert "x-access-token:••••••@github.com/org/app" in text


def test_output_without_secrets_streams_immediately():
    sink: list[str] = []
    out = _Output([], sink.append)
    out.feed(b"progress 50%")
    assert sink == ["progress 50%"]


def test_old_finished_runs_are_pruned():
    manager = JobManager()
    for i in range(JobManager.KEEP_FINISHED + 5):
        run = manager.submit(Job(f"j{i}", None, [Step("noop", action=lambda _c: None)]))
        assert run.state is RunState.SUCCEEDED  # in-process step finishes synchronously
    assert len(manager.runs) == JobManager.KEEP_FINISHED
    assert manager.runs[0].job.title == f"j{JobManager.KEEP_FINISHED + 4}"  # newest kept


def test_progress_streams_live_even_with_a_secret_configured():
    sink: list[str] = []
    out = _Output(["ghp_0123456789"], sink.append)  # 14 chars → hold back at most 13
    out.feed(b"building 10%\rbuilding 55%")
    assert sink == ["building 10%"]  # streamed without waiting for a newline; only 13 chars held
    out.flush()
    assert sink[-1] == "building 55%"
