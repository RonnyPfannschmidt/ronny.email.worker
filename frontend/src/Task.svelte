<script>
  import { taskIsLive } from "./api.js";
  let { task, csrf } = $props();
  const VERBS = { apply_bundle: "applying", fetch_body: "fetching" };
</script>

<div class="task small">
  {#if task.status === "queued"}
    <span class="muted">waiting to start</span>
  {:else if taskIsLive(task)}
    <span>
      {VERBS[task.kind] ?? "working"} — {task.done}{task.total ? `/${task.total}` : ""}{task.note
        ? ` · ${task.note}`
        : ""}
    </span>
    {#if task.total}<progress value={task.done} max={task.total}></progress>{/if}
  {:else if task.status === "failed"}
    <div class="banner error">
      {task.error}
      <form method="post" action="/task/{task.id}/retry" class="inline">
        <input type="hidden" name="_csrf" value={csrf} />
        <button>retry</button>
      </form>
    </div>
  {:else}
    <span class="ok">done{task.result?.note ? ` — ${task.result.note}` : ""}</span>
  {/if}
</div>
