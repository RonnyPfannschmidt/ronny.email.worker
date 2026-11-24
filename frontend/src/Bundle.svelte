<script>
  import { onMount } from "svelte";
  import { get, listen, post } from "./api.js";
  import Item from "./Item.svelte";

  let { bundleId, csrf } = $props();
  let bundle = $state(null);
  let error = $state("");

  const items = $derived(bundle?.items ?? []);
  const included = $derived(items.filter((item) => item.status === "proposed").length);

  async function load() {
    try {
      bundle = await get(`/next/api/bundle/${bundleId}`);
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  }

  async function change(item, path, apply) {
    item.problem = "";
    try {
      apply(await post(path, csrf));
    } catch (problem) {
      item.problem = problem.message;
    }
  }

  const fetchBody = (item) =>
    change(item, `/next/api/message/${item.message_id}/body`, (task) => (item.task = task));

  const exclude = (item) =>
    change(item, `/next/api/bundle/${bundleId}/exclude/${item.suggestion_id}`, (after) => {
      item.status = after.status;
      item.problem = after.problem;
    });

  async function taskChanged(task) {
    if (task.kind === "apply_bundle" && task.subject_id === bundleId) return load();
    if (task.kind !== "fetch_body") return;
    const item = items.find((each) => each.message_id === task.subject_id);
    if (!item) return;
    item.task = task;
    if (task.status === "done") item.body = await get(`/next/api/message/${item.message_id}/body`);
  }

  onMount(() => listen(load, taskChanged));
</script>

{#if error}<div class="banner error">{error}</div>{/if}

{#if !bundle}
  <p class="muted">loading</p>
{:else}
  <h1 class="effect">
    {bundle.operation}
    {#if bundle.target_container}<span class="arrow">→</span>{bundle.target_container}{/if}
    <span class="muted small">({included} of {items.length} messages)</span>
  </h1>
  <p class="small muted">Proposed by {bundle.producer} · status {bundle.status}</p>
  <table>
    <thead>
      <tr><th>From</th><th>Subject</th><th>Now in</th><th>Would be in</th><th></th></tr>
    </thead>
    <tbody>
      {#each items as item (item.suggestion_id)}
        <Item {item} open={bundle.status === "proposed"} {fetchBody} {exclude} />
      {/each}
    </tbody>
  </table>
  <h2>What the producer says</h2>
  <p>{bundle.summary}</p>
  <p class="muted">{bundle.reason}</p>
  {#if bundle.status === "proposed"}
    <div class="actions">
      <form method="post" action="/bundle/{bundleId}/accept">
        <input type="hidden" name="_csrf" value={csrf} />
        <input type="hidden" name="reviewed_through" value={bundle.reviewed_through} />
        <button>Accept — do this to the mailbox</button>
      </form>
      <form method="post" action="/bundle/{bundleId}/reject">
        <input type="hidden" name="_csrf" value={csrf} />
        <input name="reason" placeholder="why not (optional)" size="30" />
        <button>Reject</button>
      </form>
    </div>
  {/if}
{/if}
