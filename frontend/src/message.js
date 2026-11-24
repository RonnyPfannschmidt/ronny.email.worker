import { mount } from "svelte";
import Message from "./Message.svelte";

const target = document.getElementById("text");
mount(Message, {
  target,
  props: { messageId: Number(target.dataset.message), csrf: target.dataset.csrf },
});
