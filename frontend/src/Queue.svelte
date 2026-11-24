<script>
  import { onMount } from "svelte";
  import { get, listen, oneAtATime, post, taskIsLive } from "./api.js";
  import Effect from "./Effect.svelte";
  import Task from "./Task.svelte";

  let { csrf } = $props();
  let queue = $state(null);
  let error = $state("");

  const load = oneAtATime(async () => {
    try {
      queue = await get("/api/queue");
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  });

  function taskChanged(task) {
    if (task.kind !== "apply_bundle") return;
    const bundle = queue?.applying.find((each) => each.bundle_id === task.subject_id);
    if (bundle && taskIsLive(task)) bundle.task = task;
    else load();
  }

  async function retry(task) {
    try {
      await post(`/api/task/${task.id}/retry`, csrf);
      await load();
    } catch (problem) {
      error = problem.message;
    }
  }

  onMount(() => listen(load, taskChanged));
</script>

<h1>Awaiting review</h1>
{#if error}<div class="banner error">{error}</div>{/if}

{#if !queue}
  <p class="muted">loading</p>
{:else}
  {#if queue.elsewhere.length}
    <p class="small muted">
      Also waiting: {queue.elsewhere.map((each) => `${each.waiting} in ${each.name}`).join(", ")} —
      bundles expire if nobody reviews them.
    </p>
  {/if}

  {#if queue.bundles.length}
    <table>
      <thead>
        <tr><th>What would happen</th><th>Messages</th><th>From</th><th>Why</th><th></th></tr>
      </thead>
      <tbody>
        {#each queue.bundles as bundle (bundle.bundle_id)}
          <tr>
            <td class="effect"><Effect {bundle} /></td>
            <td>
              {bundle.item_count}
              {#if bundle.stale_count}<span class="warn small">{bundle.stale_count} moved</span>{/if}
            </td>
            <td class="small">{bundle.producer}</td>
            <td class="small muted">{bundle.summary}</td>
            <td><a href="/bundle/{bundle.bundle_id}">review</a></td>
          </tr>
        {/each}
      </tbody>
    </table>
  {:else}
    <p class="muted">Nothing to review{queue.account ? ` in ${queue.account}` : ""}.</p>
  {/if}

  {#if queue.applying.length}
    <h2>Being applied</h2>
    <table>
      <thead>
        <tr><th>What is happening</th><th>Messages</th><th>Progress</th><th></th></tr>
      </thead>
      <tbody>
        {#each queue.applying as bundle (bundle.bundle_id)}
          <tr>
            <td class="effect"><Effect {bundle} /></td>
            <td>{bundle.item_count}</td>
            <td>
              {#if bundle.task}
                <Task task={bundle.task} {retry} />
              {:else}<span class="muted small">waiting for the runner</span>{/if}
            </td>
            <td><a href="/bundle/{bundle.bundle_id}">detail</a></td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}

  {#if queue.recent.length}
    <h2>Recently decided</h2>
    <table class="small">
      <thead><tr><th>What</th><th>Messages</th><th>Outcome</th><th></th></tr></thead>
      <tbody>
        {#each queue.recent as bundle (bundle.bundle_id)}
          <tr>
            <td class="effect"><Effect {bundle} /></td>
            <td>{bundle.item_count}</td>
            <td class:ok={bundle.status === "applied"} class:warn={bundle.status === "partially_applied"}>
              {bundle.status}
            </td>
            <td><a href="/bundle/{bundle.bundle_id}">detail</a></td>
          </tr>
        {/each}
      </tbody>
    </table>
  {/if}
{/if}
