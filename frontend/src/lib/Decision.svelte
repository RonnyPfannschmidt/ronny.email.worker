<script>
  import Task from "./Task.svelte";
  import { when } from "./when.js";

  let { bundle, items, noun, grown, retry, decide, reread, next } = $props();
  const stale = $derived(items.filter((item) => item.status === "stale").length);

  let acknowledged = $state(false);
  let reason = $state("");
  let deciding = $state(false);
  let problem = $state("");

  // A tick says what was read, so it does not outlive another item going stale.
  let ticked = -1;
  $effect(() => {
    if (ticked >= 0 && stale !== ticked) acknowledged = false;
    ticked = stale;
  });

  async function settle(event, how, fields) {
    event.preventDefault();
    deciding = true;
    problem = "";
    try {
      problem = (await decide(how, fields)) ?? "";
    } catch (failed) {
      problem = failed.message;
    } finally {
      deciding = false;
    }
  }

  const accept = (event) =>
    settle(event, "accept", [
      ["reviewed_through", bundle.reviewed_through],
      ...(acknowledged ? [["acknowledge_stale", "1"]] : []),
    ]);
  const reject = (event) => settle(event, "reject", [["reason", reason]]);
</script>

{#if problem}<div class="banner error">Not done: {problem}</div>{/if}

{#if bundle.attempts.length}
  <h2>What happened</h2>
  <table class="small">
    <thead><tr><th>item</th><th>outcome</th><th>guarantee</th><th>detail</th></tr></thead>
    <tbody>
      {#each bundle.attempts as attempt}
        <tr>
          <td>{attempt.suggestion_id}</td>
          <td class={attempt.outcome === "applied" ? "ok" : "warn"}>{attempt.outcome}</td>
          <td>
            {attempt.guarantee_obtained}
            {#if attempt.guarantee_obtained !== attempt.precondition}
              <span class="muted">(asked for {attempt.precondition})</span>
            {/if}
          </td>
          <td class="muted">{attempt.detail ?? ""}</td>
        </tr>
      {/each}
    </tbody>
  </table>
  <p class="small muted">
    A best-effort guarantee means the server could not promise the message was untouched between
    the check and the change — it does not mean it was.
  </p>
{/if}

{#if bundle.status === "proposed"}
  <div class="actions">
    <form onsubmit={accept}>
      {#if stale}
        <p class="small">
          <label>
            <input type="checkbox" bind:checked={acknowledged} />
            I have read what changed on the {stale} message(s) above. They will not be applied.
          </label>
        </p>
      {/if}
      {#if grown}
        <p class="small warn">
          More {noun} arrived. <button type="button" onclick={reread}>Read it again</button> before
          accepting.
        </p>
      {/if}
      <button disabled={grown || deciding || (stale && !acknowledged)}>
        Accept — do this to the mailbox
      </button>
    </form>
    <form onsubmit={reject}>
      <input bind:value={reason} placeholder="why not (optional)" size="30" />
      <button disabled={deciding}>Reject</button>
    </form>
  </div>
{:else if bundle.status === "accepted"}
  <div class="banner">
    Accepted — being applied to the mailbox now. Nothing here needs you until it finishes.
  </div>
  {#if bundle.apply_task}
    <Task task={bundle.apply_task} {retry} />
  {:else}
    <p class="small muted">waiting for the runner</p>
  {/if}
{:else if bundle.status === "stale"}
  <p class="small warn">
    Every message this referred to moved on before anybody decided, so there is nothing left to
    apply and this closed itself. Nobody rejected it — see what changed against each message above.
  </p>
{:else}
  <p class="small muted">
    {bundle.status}{bundle.decided_by ? ` by ${bundle.decided_by}` : ""}{bundle.decided_at
      ? ` at ${when(bundle.decided_at)}`
      : ""}{bundle.decision_reason ? ` — ${bundle.decision_reason}` : ""}
  </p>
{/if}

{#if bundle.status !== "proposed" && next}
  <p><a href="/bundle/{next}">On to the next waiting bundle</a> <span class="small muted">(n)</span></p>
{/if}
