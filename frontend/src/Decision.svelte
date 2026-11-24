<script>
  import Task from "./Task.svelte";
  import { when } from "./when.js";

  let { bundle, csrf, items, retry } = $props();
  const stale = $derived(items.filter((item) => item.status === "stale").length);
</script>

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
    <form method="post" action="/bundle/{bundle.bundle_id}/accept">
      <input type="hidden" name="_csrf" value={csrf} />
      <input type="hidden" name="reviewed_through" value={bundle.reviewed_through} />
      {#if stale}
        <p class="small">
          <label>
            <input type="checkbox" name="acknowledge_stale" value="1" required />
            I have read what changed on the {stale} message(s) above. They will not be applied.
          </label>
        </p>
      {/if}
      <button>Accept — do this to the mailbox</button>
    </form>
    <form method="post" action="/bundle/{bundle.bundle_id}/reject">
      <input type="hidden" name="_csrf" value={csrf} />
      <input name="reason" placeholder="why not (optional)" size="30" />
      <button>Reject</button>
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
