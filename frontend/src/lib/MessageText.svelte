<script>
  import { onMount } from "svelte";
  import { cache, listen, oneAtATime, post, read, taskIsLive } from "./api.js";

  let { messageId } = $props();
  // Fixed for the life of the view: another message is another view.
  // svelte-ignore state_referenced_locally
  const TEXT = `/api/message/${messageId}`;
  let message = $state(cache.get(TEXT) ?? null);
  let error = $state("");

  const body = $derived(message?.body);
  const task = $derived(message?.task);

  const load = oneAtATime(async () => {
    try {
      message = await read(TEXT);
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  });

  async function fetchText() {
    try {
      message.task = await post(`/api/message/${messageId}/body`);
      error = "";
    } catch (problem) {
      error = problem.message;
    }
  }

  function taskChanged(changed) {
    if (changed.kind !== "fetch_body" || changed.subject_id !== messageId) return;
    if (message && taskIsLive(changed)) message.task = changed;
    else load();
  }

  onMount(() => listen(load, taskChanged));
</script>

{#if error}<div class="banner error">{error}</div>{/if}

{#if body}
  {#if body.text}
    <div class="body">{body.text}</div>
  {:else}
    <p class="muted">This message has no text part.</p>
  {/if}
  {#if body.links.length}
    <details>
      <summary class="small">{body.links.length} links</summary>
      <table class="links">
        <tbody>
          {#each body.links as link}
            <tr><td>{link.text}</td><td class="muted">{link.target}</td></tr>
          {/each}
        </tbody>
      </table>
    </details>
  {/if}
  {#if body.attachments.length}
    <p class="small muted">
      Attachments: {body.attachments.join(", ")} — named, not fetched. Open them in your own client.
    </p>
  {/if}
{:else if taskIsLive(task)}
  <p class="small muted">fetching</p>
{:else if message}
  {#if task?.status === "failed"}
    <div class="banner error">{task.error}</div>
  {:else if task?.result?.note}
    <p class="small warn">{task.result.note}</p>
  {/if}
  <p>
    <button onclick={fetchText}>
      {task?.status === "failed" ? "try again" : "fetch the text"}
    </button>
    <span class="small muted">Only the text parts, and no remote content is ever loaded.</span>
  </p>
{/if}
