<script>
  import { taskIsLive } from "./api.js";
  let { task, retry } = $props();
  const VERBS = {
    apply_bundle: "applying",
    sync_account: "syncing",
    sync_container: "syncing",
    fetch_body: "fetching",
  };
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
      <button onclick={() => retry(task)}>retry</button>
    </div>
  {:else}
    <span class="ok">done{task.result?.note ? ` — ${task.result.note}` : ""}</span>
  {/if}
</div>
