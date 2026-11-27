<script>
  import { taskIsLive } from "./api.js";
  import { day } from "./when.js";

  let { item, bundle, open, fetchBody, exclude } = $props();
  const destination = $derived(
    { move: item.would_move_to, delete: "Trash" }[bundle.operation] ?? item.currently_in,
  );
</script>

<tr id="m{item.message_id}" class:stale={item.status === "stale"}>
  <td class="small">
    {#if item.arrived_late}<span class="muted small">added later</span><br />{/if}
    {item.from_address ?? "—"}
    {#if item.from_display_name}<br /><span class="muted">{item.from_display_name}</span>{/if}
  </td>
  <td class="small">{item.subject ?? "(no subject)"}</td>
  <td class="small muted">{day(item.date)}</td>
  <td class="small">{item.currently_in}</td>
  <td class="arrow">→</td>
  <td class="small">
    {destination}
    {#if bundle.flag}<span class="muted">({bundle.operation} {bundle.flag})</span>{/if}
  </td>
  <td class="small">
    {#if open && item.status === "proposed"}
      <button onclick={() => exclude(item)}>exclude</button>
    {:else}{item.status}{/if}
  </td>
</tr>
{#if item.stale_detail}
  <tr><td colspan="7" class="small warn">{item.stale_detail}</td></tr>
{/if}
<tr>
  <td colspan="7">
    {#if item.problem}<div class="banner error">{item.problem}</div>{/if}
    {#each item.findings as finding}
      <span class="finding {finding.class}">{finding.detail}</span>
    {/each}
    <details>
      <summary class="small muted">the message itself</summary>
      {#if item.body}
        <div class="body">{item.body.text}</div>
        {#if item.body.links.length}
          <table class="links">
            <thead><tr><th>link text</th><th>actually goes to</th></tr></thead>
            <tbody>
              {#each item.body.links as link}
                <tr><td>{link.text}</td><td>{link.target}</td></tr>
              {/each}
            </tbody>
          </table>
        {/if}
        <p class="small muted">
          Nothing above was fetched from the network. Remote images are not loaded: loading one
          tells the sender the mail was read.
        </p>
      {:else if taskIsLive(item.task)}
        <span class="small muted">fetching</span>
      {:else}
        {#if item.task?.status === "failed"}
          <div class="banner error">{item.task.error}</div>
        {:else if item.task?.result?.note}
          <p class="small warn">{item.task.result.note}</p>
        {/if}
        <button onclick={() => fetchBody(item)}>
          {item.task?.status === "failed" ? "try again" : "fetch the body from the server"}
        </button>
      {/if}
    </details>
  </td>
</tr>
