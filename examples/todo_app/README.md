# Example: Todo App

Three-task DAG: **design -> build -> review**. Uses the stub conductor so
the example runs end-to-end with no LLM calls. Run it with:

```bash
python examples/todo_app/run.py
```

Expected output (paths and ids vary):

```
submitted: design (b0f6a...)
submitted: build  (b0f6c...)  blocked_by=design
submitted: review (b0f6d...)  blocked_by=build
unblocked initially: ['design']
running supervisor...
{
  "kanban": {"pending": 0, "in_progress": 0, "done": 3, "failed": 0},
  ...
}
```
