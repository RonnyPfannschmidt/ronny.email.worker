<script>
  import { onMount } from "svelte";
  import { page } from "$app/state";
  import { cache, listen, missing, oneAtATime, read } from "#lib/api.js";
  import Filing from "#lib/Filing.svelte";
  import { when } from "#lib/when.js";

  const containerId = Number(page.params.id);
  const offset = Math.max(Number(page.url.searchParams.get("offset")) || 0, 0);
  const unread = page.url.searchParams.get("unread") === "1";
  const FOLDER = `/api/mail/${containerId}?offset=${offset}&unread=${unread}`;

  let folder = $state(cache.get(FOLDER) ?? null);
  let error = $state("");
  let picked = $state([]);

  const listing = $derived(folder?.listing);
  const here = (at, onlyUnread = unread) =>
    `/mail/${containerId}?${new URLSearchParams({
      ...(at ? { offset: at } : {}),
      ...(onlyUnread ? { unread: "1" } : {}),
    })}`.replace(/\?$/, "");

  const load = oneAtATime(async () => {
    try {
      folder = await read(FOLDER);
      error = "";
    } catch (problem) {
      error = missing(problem, "such folder");
    }
  });

  const synced = (task) => {
    if (task.kind.startsWith("sync") && task.status === "done") load();
  };

  onMount(() => listen(load, synced));
</script>

<svelte:head><title>{folder?.container.name ?? "folder"} — mailmind</title></svelte:head>

{#if error}<div class="banner error">{error}</div>{/if}

{#if !folder}
  <p class="muted">loading</p>
{:else}
  <h1>{folder.container.name}</h1>
  <p class="small muted">
    <a href="/mail">all folders</a> ·
    {#if unread}<a href={here(0, false)}>everything</a>
    {:else}<a href={here(0, true)}>unread only</a>{/if} ·
    showing {listing.returned} of {listing.total_matching}
  </p>

  {#if !listing.messages.length}
    <p class="muted">Nothing here.</p>
  {:else}
    <table>
      <thead><tr><th></th><th>From</th><th>Subject</th><th>Date</th></tr></thead>
      <tbody>
        {#each listing.messages as message (message.message_id)}
          <tr>
            <td>
              <input
                type="checkbox"
                aria-label="pick {message.subject ?? 'message'}"
                value={message.message_id}
                bind:group={picked}
              />
            </td>
            <!-- The address, and the display name under it rather than instead of it: 08 —
                 identity is the address, and the disagreement between the two is a finding. -->
            <td class="small">
              {message.from_address ?? "—"}
              {#if message.from_display_name}
                <span class="finding muted">{message.from_display_name}</span>
              {/if}
            </td>
            <td>
              <a href="/mail/message/{message.message_id}" class:here={!message.flags.includes("\\Seen")}>
                {message.subject || "(no subject)"}
              </a>
              {#if message.has_attachments}<span class="muted small">attachment</span>{/if}
              {#if message.list_id}<span class="muted small">{message.list_id}</span>{/if}
              {#if message.preview}
                <span class="finding muted">{message.preview.slice(0, 160)}</span>
              {/if}
            </td>
            <td class="small muted effect">{when(message.date)}</td>
          </tr>
        {/each}
      </tbody>
    </table>
    <Filing {containerId} folders={folder.folders} {picked} them="the picked messages" />
  {/if}

  <p class="small">
    {#if offset}<a href={here(Math.max(offset - folder.page, 0))}>newer</a>{/if}
    {#if offset + listing.returned < listing.total_matching}
      <a href={here(offset + folder.page)}>older</a>
    {/if}
  </p>
{/if}
