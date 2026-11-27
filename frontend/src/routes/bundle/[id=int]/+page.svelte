<script>
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";
  import { page } from "$app/state";
  import { cache, listen, missing, oneAtATime, post, read } from "#lib/api.js";
  import Banners from "#lib/Banners.svelte";
  import Decision from "#lib/Decision.svelte";
  import FolderItem from "#lib/FolderItem.svelte";
  import Item from "#lib/Item.svelte";
  import { when } from "#lib/when.js";

  const bundleId = Number(page.params.id);
  const BUNDLE = `/api/bundle/${bundleId}`;
  const QUEUE = "/api/queue";

  let bundle = $state(null);
  let error = $state("");
  let grown = $state(false);
  let closed = $state(false);
  let waiting = $state(cache.get(QUEUE)?.bundles ?? []);
  // What came from the cache was drawn before this visit read anything, so the first fresh
  // read replaces it: nobody has read it yet, and nothing is slipped under them.
  let settled = false;

  const items = $derived(bundle?.items ?? []);
  const folders = $derived(bundle?.operation === "discard_container");
  const open = $derived(bundle?.status === "proposed");
  const noun = $derived(folders ? "folders" : "messages");
  const next = $derived.by(() => {
    const ids = waiting.map((each) => each.bundle_id);
    const here = ids.indexOf(bundleId);
    return [...ids.slice(here + 1), ...ids.slice(0, Math.max(here, 0))].find(
      (id) => id !== bundleId,
    );
  });

  function take(fresh) {
    // A reader is never handed items under their eyes: what is on the page stays the page,
    // and so does the `reviewed_through` that accepting it sends.
    if (
      settled &&
      bundle &&
      fresh.status === "proposed" &&
      fresh.reviewed_through !== bundle.reviewed_through
    ) {
      grown = true;
      return;
    }
    grown = false;
    closed ||= settled && bundle?.status === "proposed" && !["proposed", "accepted"].includes(fresh.status);
    // Kept rather than replaced: a click still waiting for its answer holds the old item.
    const shown = new Map(items.map((item) => [item.suggestion_id, item]));
    bundle = {
      ...fresh,
      items: fresh.items.map((item) => {
        const old = shown.get(item.suggestion_id);
        return old ? Object.assign(old, item) : item;
      }),
    };
  }

  const load = oneAtATime(async () => {
    try {
      const fresh = await read(BUNDLE);
      take(fresh);
      settled = true;
      error = "";
    } catch (problem) {
      error = missing(problem, `bundle ${bundleId}`);
    }
  });

  const loadQueue = oneAtATime(async () => {
    try {
      waiting = (await read(QUEUE)).bundles;
    } catch {
      // Only the way on is lost; the bundle itself still shows.
    }
  });

  if (cache.has(BUNDLE)) take(cache.get(BUNDLE));

  /** Reads what is there now, as if arriving: the banner asking for it goes. */
  function reread() {
    settled = false;
    grown = false;
    load();
  }

  /** Accepting or rejecting, here: the answer is the bundle, drawn again in place. */
  async function decide(how, fields) {
    const { problem } = await post(`/api/bundle/${bundleId}/${how}`, fields);
    await Promise.all([load(), loadQueue()]);
    return problem;
  }

  async function retry(task) {
    try {
      bundle.apply_task = await post(`/api/task/${task.id}/retry`);
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  }

  async function change(item, path, apply) {
    item.problem = "";
    try {
      apply(await post(path));
    } catch (problem) {
      item.problem = problem.message;
    }
  }

  const fetchBody = (item) =>
    change(item, `/api/message/${item.message_id}/body`, (task) => (item.task = task));

  const exclude = (item) =>
    change(item, `/api/bundle/${bundleId}/exclude/${item.suggestion_id}`, (after) => {
      item.status = after.status;
      item.problem = after.problem;
    });

  async function taskChanged(task) {
    if (!bundle) return;
    if (task.kind === "apply_bundle" && task.subject_id === bundleId) {
      bundle.apply_task = task;
      if (["done", "failed"].includes(task.status)) await load();
      return;
    }
    if (task.kind !== "fetch_body") return;
    const item = items.find((each) => each.message_id === task.subject_id);
    if (!item) return;
    item.task = task;
    if (task.status !== "done" || task.result?.note) return;
    try {
      item.body = await read(`/api/message/${item.message_id}/body`);
    } catch (problem) {
      item.problem = problem.message;
    }
  }

  function queueChanged(bundles) {
    if (bundles.includes(bundleId)) load();
    loadQueue();
  }

  function key(event) {
    if (event.key !== "n" || !next || event.ctrlKey || event.altKey || event.metaKey) return;
    if (event.target.matches("input, textarea, select")) return;
    event.preventDefault();
    goto(`/bundle/${next}`);
  }

  onMount(() => {
    loadQueue();
    return listen(load, taskChanged, queueChanged);
  });
</script>

<svelte:window onkeydown={key} />
<svelte:head><title>review — mailmind</title></svelte:head>

{#if error}<div class="banner error">{error}</div>{/if}

{#if !bundle}
  <p class="muted">loading</p>
{:else}
  <p class="small muted">
    <a href="/">the queue</a>
    {#if next}· <a href="/bundle/{next}" class="next">next waiting bundle</a> (n){/if}
  </p>
  <h1 class="effect">
    {folders ? "discard" : bundle.operation}{bundle.flag ? ` ${bundle.flag}` : ""}
    {#if bundle.target_container}<span class="arrow">→</span> {bundle.target_container}{/if}
    {#if bundle.target_is_new}
      <span class="muted small">(new folder — will be created)</span>
    {/if}
    <span class="muted small">({items.length} {noun})</span>
  </h1>

  <Banners {bundle} {folders} {noun} {grown} {closed} {reread} />

  <p class="small muted">
    Proposed by {bundle.producer} · status {bundle.status} · expires {when(bundle.expires_at)}
  </p>

  <h2>The effect</h2>
  {#if folders}
    <table>
      <thead>
        <tr><th>Folder</th><th>Holds</th><th>Under it</th><th></th><th>Would be</th><th></th></tr>
      </thead>
      <tbody>
        {#each items as item (item.suggestion_id)}<FolderItem {item} {open} {exclude} />{/each}
      </tbody>
    </table>
    <p class="small muted">
      A folder listed under another one goes after it does, so a whole branch can be removed in
      one bundle without the server ever being asked to delete a folder that still has something
      below it.
    </p>
  {:else}
    <table>
      <thead>
        <tr>
          <th>From</th><th>Subject</th><th>Date</th><th>Now in</th><th></th><th>Would be in</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {#each items as item (item.suggestion_id)}
          <Item {item} {bundle} {open} {fetchBody} {exclude} />
        {/each}
      </tbody>
    </table>
  {/if}

  <h2>What the producer says</h2>
  <p>{bundle.summary}</p>
  <p class="muted">{bundle.reason}</p>
  <p class="small muted">
    The table above is what it would actually do — decide from that, not from this.
  </p>

  <Decision {bundle} {items} {noun} {grown} {retry} {decide} {reread} {next} />
{/if}
