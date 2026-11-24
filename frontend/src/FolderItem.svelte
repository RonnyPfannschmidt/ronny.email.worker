<script>
  let { item, open, exclude } = $props();
</script>

<tr id="s{item.suggestion_id}" class:stale={item.status === "stale"}>
  <td class="small">{item.container}</td>
  <td class="small" class:warn={item.cached_messages}>{item.cached_messages || "nothing"}</td>
  <td class="small muted">{item.children.length ? item.children.join(", ") : "—"}</td>
  <td class="arrow">→</td>
  <td class="small">
    gone{#if !item.exists_on_server}{" "}<span class="muted">(never made on the server)</span>{/if}
  </td>
  <td class="small">
    {#if open && item.status === "proposed"}
      <button onclick={() => exclude(item)}>exclude</button>
    {:else}{item.status}{/if}
  </td>
</tr>
{#if item.problem || item.stale_detail}
  <tr>
    <td colspan="6" class="small warn">{item.problem || item.stale_detail}</td>
  </tr>
{/if}
