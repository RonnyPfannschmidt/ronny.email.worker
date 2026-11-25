<script>
  import { onMount } from "svelte";
  import { get, listen, oneAtATime, post, taskIsLive } from "./api.js";
  import Effect from "./Effect.svelte";
  import Task from "./Task.svelte";

  let { csrf } = $props();
  let queue = $state(null);
  let error = $state("");
  let picked = $state([]);
  let reason = $state("");
  let rejecting = $state(false);
  let refused = $state([]);
  let held = $state([]);
  let body = $state(null);
  // Where the keyboard is, kept so that a row leaving does not lose the place.
  let at = -1;

  const ids = (bundles) => bundles.map((each) => each.bundle_id);
  const rows = $derived((queue?.bundles ?? []).filter((each) => !held.includes(each.bundle_id)));
  // Only what is on the page: a bundle decided elsewhere leaves with its tick.
  const chosen = $derived(ids(rows).filter((id) => picked.includes(id)));
  const several = (count) => `${count} bundle${count === 1 ? "" : "s"}`;

  function take(fresh) {
    // Rows do not move under a pointer that is ticking them: while something is ticked,
    // what arrives waits to be asked for.
    const shown = ids(rows);
    const arrived = ids(fresh.bundles).filter((id) => !shown.includes(id));
    const ticking = ids(fresh.bundles).some((id) => chosen.includes(id));
    held = ticking ? arrived : [];
    queue = fresh;
  }

  const load = oneAtATime(async () => {
    try {
      take(await get("/api/queue"));
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

  async function reject(event) {
    event.preventDefault();
    rejecting = true;
    error = "";
    try {
      const fields = chosen.map((id) => ["bundle", id]);
      ({ refused } = await post("/api/queue/reject", csrf, [...fields, ["reason", reason]]));
      picked = [];
      reason = "";
      await load();
    } catch (problem) {
      error = problem.message;
    } finally {
      rejecting = false;
    }
  }

  function go(to) {
    const boxes = body.querySelectorAll("input");
    at = Math.max(0, Math.min(to, boxes.length - 1));
    boxes[at].focus();
  }

  function key(event) {
    if (!body || event.ctrlKey || event.altKey || event.metaKey || event.isComposing) return;
    const { target } = event;
    if (target.matches("input:not([type=checkbox]), textarea, select")) return;
    const row = target.closest("tbody") === body ? target.closest("tr") : null;
    if (row) at = [...body.rows].indexOf(row);
    if (event.key === "j") go(row ? at + 1 : at);
    else if (event.key === "k") go(row ? at - 1 : at);
    else if (event.key === "x" && row && !event.repeat) row.querySelector("input").click();
    else if (event.key === "Enter" && target.matches("[type=checkbox]") && row)
      row.querySelector("a").click();
    else return;
    event.preventDefault();
  }

  onMount(() => listen(load, taskChanged, load));
</script>

<svelte:window onkeydown={key} />

<h1>Awaiting review</h1>
{#each refused as each (each.bundle_id)}
  <div class="banner error">
    Not rejected: <a href="/bundle/{each.bundle_id}">bundle {each.bundle_id}</a> — {each.problem}
  </div>
{/each}
{#if error}<div class="banner error">{error}</div>{/if}

{#if !queue}
  <p class="muted">loading</p>
{:else}
  {#if rows.length}
    <table>
      <thead>
        <tr>
          <th></th><th>What would happen</th><th>Messages</th><th>From</th><th>Why</th><th></th>
        </tr>
      </thead>
      <tbody class="keyed" bind:this={body}>
        {#each rows as bundle (bundle.bundle_id)}
          <tr>
            <td>
              <input
                type="checkbox"
                aria-label="choose bundle {bundle.bundle_id}"
                value={bundle.bundle_id}
                bind:group={picked}
                disabled={rejecting}
              />
            </td>
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
    <form class="actions" onsubmit={reject}>
      <input bind:value={reason} placeholder="why not (optional)" size="30" />
      <button disabled={!chosen.length || rejecting}>Reject {several(chosen.length)}</button>
      {#if held.length}
        <button type="button" onclick={() => (held = [])}>Show {held.length} new</button>
      {/if}
      <span class="small muted">
        To accept one, review it. Keys: j and k move, x ticks, Enter reviews.
      </span>
    </form>
  {:else}
    <p class="muted">Nothing to review{queue.account ? ` in ${queue.account}` : ""}.</p>
  {/if}

  <!-- Below the table: a line appearing above it would move the rows. -->
  {#if queue.elsewhere.length}
    <p class="small muted">
      Also waiting: {queue.elsewhere.map((each) => `${each.waiting} in ${each.name}`).join(", ")} —
      bundles expire if nobody reviews them.
    </p>
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
