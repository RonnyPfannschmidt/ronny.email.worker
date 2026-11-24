import { mount } from "svelte";
import Bundle from "./Bundle.svelte";

const target = document.getElementById("app");
mount(Bundle, {
  target,
  props: { bundleId: Number(target.dataset.bundle), csrf: target.dataset.csrf },
});
