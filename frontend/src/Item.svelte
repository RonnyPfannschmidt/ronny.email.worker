<script>
  let { item, open, fetchBody, exclude } = $props();
  const busy = $derived(["queued", "running"].includes(item.task?.status));
</script>

<tr id="m{item.message_id}" class:stale={item.status === "stale"}>
  <td class="small">{item.from_address ?? "—"}</td>
  <td class="small">{item.subject ?? "(no subject)"}</td>
  <td class="small">{item.currently_in}</td>
  <td class="small">{item.would_move_to ?? "—"}</td>
  <td class="small">
    {#if open && item.status === "proposed"}
      <button onclick={() => exclude(item)}>exclude</button>
    {:else}{item.status}{/if}
  </td>
</tr>
<tr>
  <td colspan="5">
    {#if item.problem}<div class="banner error">{item.problem}</div>{/if}
    {#if item.stale_detail}<span class="small warn">{item.stale_detail}</span>{/if}
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
              {#each item.body.links as link}<tr><td>{link.text}</td><td>{link.target}</td></tr>{/each}
            </tbody>
          </table>
        {/if}
      {:else if busy}
        <span class="small muted">fetching</span>
      {:else}
        {#if item.task?.status === "failed"}
          <div class="banner error">{item.task.error}</div>
        {/if}
        <button onclick={() => fetchBody(item)}>
          {item.task?.status === "failed" ? "try again" : "fetch the body from the server"}
        </button>
      {/if}
    </details>
  </td>
</tr>
