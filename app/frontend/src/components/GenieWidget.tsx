import { useEffect, useRef, useState } from "react";
import {
  ActionIcon,
  Alert,
  Anchor,
  Box,
  Collapse,
  Group,
  Button,
  Loader,
  Paper,
  Popover,
  ScrollArea,
  Stack,
  Table,
  Text,
  Textarea,
  Tooltip,
  UnstyledButton,
} from "@mantine/core";
import { useDisclosure } from "@mantine/hooks";
import { useQuery } from "@tanstack/react-query";
import {
  IconArrowsDiagonal,
  IconArrowsDiagonalMinimize2,
  IconChevronRight,
  IconExternalLink,
  IconMessageChatbot,
  IconPencilPlus,
  IconSend,
  IconX,
} from "@tabler/icons-react";

import {
  getConfig,
  getGenieMessage,
  sendGenieMessage,
  startGenieConversation,
  type GenieResult,
} from "../api/client";

// T5 — Genie chat as a floating overlay (not a route), mounted in the app shell.
//
// The backend is stateless: we hold conversation_id here and pass it back on follow-ups
// (which is what preserves Genie's context). Genie's API is async, so after submitting a
// message we POLL get_message until the status is terminal, showing a typing indicator, and
// cap the wait at ~30s (task requirement) then surface a friendly timeout.

const POLL_MS = 1500;
const MAX_POLL_MS = 30_000;

type ChatItem =
  | { role: "user"; text: string }
  | {
      role: "genie";
      text: string | null;
      query: string | null;
      result: GenieResult | null;
      error: string | null;
    };

// Databricks returns every result value as a STRING — numeric columns come back verbatim,
// sometimes in scientific notation (e.g. "2.0382632410000004E7"). Render numeric-looking
// cells with thousands separators (up to 2 decimals) so they read like the prose answer;
// leave anything non-numeric (ids, dates, text) untouched.
function formatCell(value: string | null): string {
  if (value === null || value === "") return "—";
  const n = Number(value);
  if (!Number.isFinite(n) || value.trim() === "") return value;
  // Guard: don't reformat things that merely *parse* as numbers but are really codes
  // (leading zeros like "0042" or things with no digit-grouping intent). Only touch values
  // whose canonical number differs from the original text (i.e. E-notation / long decimals).
  if (String(n) === value) return value;
  const decimals = Number.isInteger(n) ? 0 : 2;
  return n.toLocaleString(undefined, {
    minimumFractionDigits: decimals,
    maximumFractionDigits: decimals,
  });
}

function ResultTable({ result }: { result: GenieResult }) {
  if (!result.columns.length) return null;
  return (
    <ScrollArea.Autosize mah={220} type="auto">
      <Table striped withTableBorder withColumnBorders fz="xs" stickyHeader>
        <Table.Thead>
          <Table.Tr>
            {result.columns.map((c) => (
              <Table.Th key={c}>{c}</Table.Th>
            ))}
          </Table.Tr>
        </Table.Thead>
        <Table.Tbody>
          {result.rows.map((row, i) => (
            <Table.Tr key={i}>
              {row.map((cell, j) => (
                <Table.Td key={j}>{formatCell(cell)}</Table.Td>
              ))}
            </Table.Tr>
          ))}
        </Table.Tbody>
      </Table>
      {result.truncated && (
        <Text size="xs" c="dimmed" mt={4}>
          Preview truncated — open in workspace for the full result.
        </Text>
      )}
    </ScrollArea.Autosize>
  );
}

// Genie's generated SQL, tucked behind a default-closed "Show SQL" disclosure so the answer
// stays clean but the mechanics are one click away.
function SqlBlock({ query }: { query: string }) {
  const [opened, { toggle }] = useDisclosure(false);
  return (
    <Box>
      <UnstyledButton onClick={toggle}>
        <Group gap={4} wrap="nowrap">
          <IconChevronRight
            size={14}
            style={{ transform: opened ? "rotate(90deg)" : "none", transition: "transform 150ms" }}
          />
          <Text size="xs" c="dimmed" fw={500}>
            {opened ? "Hide SQL" : "Show SQL"}
          </Text>
        </Group>
      </UnstyledButton>
      <Collapse in={opened}>
        <Text
          size="xs"
          c="dimmed"
          ff="monospace"
          mt={4}
          style={{ whiteSpace: "pre-wrap" }}
        >
          {query}
        </Text>
      </Collapse>
    </Box>
  );
}

export default function GenieWidget() {
  const [open, setOpen] = useState(false);
  const [enlarged, setEnlarged] = useState(false);
  const [input, setInput] = useState("");
  const [items, setItems] = useState<ChatItem[]>([]);
  const [busy, setBusy] = useState(false);
  const [confirmNew, setConfirmNew] = useState(false);
  const conversationId = useRef<string | null>(null);
  const cancelled = useRef(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const { data: cfg } = useQuery({ queryKey: ["config"], queryFn: getConfig, staleTime: 5 * 60_000 });
  const workspaceLink =
    cfg?.databricks_host && cfg?.genie_space_id
      ? `${cfg.databricks_host}/genie/rooms/${cfg.genie_space_id}`
      : null;

  // Stop any in-flight poll loop when the widget unmounts or the panel closes.
  useEffect(() => {
    return () => {
      cancelled.current = true;
    };
  }, []);

  useEffect(() => {
    // Auto-scroll to the newest message.
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight, behavior: "smooth" });
  }, [items, busy]);

  // Start a fresh Genie conversation: cancel any in-flight poll, drop the local history, and
  // null the conversation id so the next message calls start_conversation (not create_message,
  // which would carry the old context). No server-side delete needed — chats are ephemeral.
  function resetConversation() {
    cancelled.current = true;
    conversationId.current = null;
    setItems([]);
    setInput("");
    setBusy(false);
    setConfirmNew(false);
  }

  async function poll(convId: string, msgId: string) {
    const started = Date.now();
    // Poll get_message until terminal or the 30s cap.
    // eslint-disable-next-line no-constant-condition
    while (true) {
      if (cancelled.current) return;
      if (Date.now() - started > MAX_POLL_MS) {
        setItems((prev) => [
          ...prev,
          {
            role: "genie",
            text: null,
            query: null,
            result: null,
            error: "Genie is taking longer than expected. Try rephrasing or ask again.",
          },
        ]);
        return;
      }
      const msg = await getGenieMessage(convId, msgId);
      const terminal = ["COMPLETED", "FAILED", "CANCELLED", "QUERY_RESULT_EXPIRED"].includes(
        msg.status,
      );
      if (terminal) {
        setItems((prev) => [
          ...prev,
          {
            role: "genie",
            text: msg.content,
            query: msg.query,
            result: msg.result,
            error:
              msg.status === "COMPLETED"
                ? null
                : msg.error ?? `Genie could not answer (status: ${msg.status}).`,
          },
        ]);
        return;
      }
      await new Promise((r) => setTimeout(r, POLL_MS));
    }
  }

  async function submit() {
    const content = input.trim();
    if (!content || busy) return;
    // Re-arm polling — resetConversation() / closing the panel sets this true.
    cancelled.current = false;
    setInput("");
    setItems((prev) => [...prev, { role: "user", text: content }]);
    setBusy(true);
    try {
      const ref = conversationId.current
        ? await sendGenieMessage(conversationId.current, content)
        : await startGenieConversation(content);
      conversationId.current = ref.conversation_id;
      await poll(ref.conversation_id, ref.message_id);
    } catch (e) {
      setItems((prev) => [
        ...prev,
        {
          role: "genie",
          text: null,
          query: null,
          result: null,
          error: (e as Error).message,
        },
      ]);
    } finally {
      setBusy(false);
    }
  }

  // ---- Floating launcher (closed state) ----
  if (!open) {
    return (
      <Tooltip label="Ask Genie" position="left">
        <ActionIcon
          size={54}
          radius="xl"
          color="lava"
          variant="filled"
          onClick={() => {
            cancelled.current = false;
            setOpen(true);
          }}
          style={{ position: "fixed", bottom: 24, right: 24, boxShadow: "0 4px 14px rgba(0,0,0,0.25)" }}
          aria-label="Ask Genie"
        >
          <IconMessageChatbot size={26} />
        </ActionIcon>
      </Tooltip>
    );
  }

  // ---- Chat panel (open state) ----
  return (
    <Paper
      shadow="xl"
      radius="md"
      withBorder
      style={{
        position: "fixed",
        bottom: 24,
        right: 24,
        width: enlarged ? 720 : 380,
        height: enlarged ? 640 : 520,
        display: "flex",
        flexDirection: "column",
        overflow: "hidden",
        zIndex: 1000,
      }}
    >
      {/* Header */}
      <Group justify="space-between" px="sm" py="xs" bg="navy.7" wrap="nowrap">
        <Group gap="xs" wrap="nowrap">
          <IconMessageChatbot size={20} color="white" />
          <Text fw={600} c="white" size="sm">
            Ask Genie
          </Text>
        </Group>
        <Group gap={4} wrap="nowrap">
          {enlarged && workspaceLink && (
            <Anchor href={workspaceLink} target="_blank" c="gray.3" title="Open in workspace">
              <Group gap={4} wrap="nowrap">
                <IconExternalLink size={16} />
                <Text size="xs">Open in workspace</Text>
              </Group>
            </Anchor>
          )}
          {/* New chat: reset immediately if empty, else confirm via a small popover. */}
          <Popover
            opened={confirmNew}
            onChange={setConfirmNew}
            position="bottom-end"
            withArrow
            shadow="md"
            // The panel sits at zIndex 1000; the popover portal defaults to 300 and would
            // render BEHIND the panel (invisible). Lift it above.
            zIndex={1100}
          >
            <Popover.Target>
              <ActionIcon
                variant="subtle"
                color="gray"
                onClick={() => (items.length === 0 ? resetConversation() : setConfirmNew(true))}
                aria-label="New chat"
              >
                <IconPencilPlus size={18} color="white" />
              </ActionIcon>
            </Popover.Target>
            <Popover.Dropdown>
              <Text size="sm" mb="xs">
                Clear this conversation and start a new chat?
              </Text>
              <Group gap="xs" justify="flex-end">
                <Button size="xs" variant="default" onClick={() => setConfirmNew(false)}>
                  Cancel
                </Button>
                <Button size="xs" color="lava" onClick={resetConversation}>
                  New chat
                </Button>
              </Group>
            </Popover.Dropdown>
          </Popover>
          <ActionIcon
            variant="subtle"
            color="gray"
            onClick={() => setEnlarged((v) => !v)}
            aria-label={enlarged ? "Shrink" : "Enlarge"}
          >
            {enlarged ? (
              <IconArrowsDiagonalMinimize2 size={18} color="white" />
            ) : (
              <IconArrowsDiagonal size={18} color="white" />
            )}
          </ActionIcon>
          <ActionIcon
            variant="subtle"
            color="gray"
            onClick={() => {
              cancelled.current = true;
              setOpen(false);
            }}
            aria-label="Close"
          >
            <IconX size={18} color="white" />
          </ActionIcon>
        </Group>
      </Group>

      {/* Messages */}
      <Box ref={scrollRef} style={{ flex: 1, overflowY: "auto" }} p="sm" bg="#F6F5F2">
        <Stack gap="sm">
          {items.length === 0 && (
            <Text size="sm" c="dimmed" ta="center" mt="lg">
              Ask a question about your customers — e.g. “Top segment by LTV”.
            </Text>
          )}
          {items.map((item, i) =>
            item.role === "user" ? (
              <Paper key={i} bg="lava.5" c="white" p="xs" radius="md" ml="auto" maw="85%">
                <Text size="sm">{item.text}</Text>
              </Paper>
            ) : (
              <Paper key={i} bg="white" withBorder p="xs" radius="md" mr="auto" maw="95%">
                {item.error ? (
                  <Alert color="red" variant="light" p="xs">
                    <Text size="sm">{item.error}</Text>
                  </Alert>
                ) : (
                  <Stack gap="xs">
                    {item.text && (
                      <Text size="sm" style={{ whiteSpace: "pre-wrap" }}>
                        {item.text}
                      </Text>
                    )}
                    {item.result && <ResultTable result={item.result} />}
                    {item.query && <SqlBlock query={item.query} />}
                  </Stack>
                )}
              </Paper>
            ),
          )}
          {busy && (
            <Group gap="xs" ml="xs">
              <Loader size="xs" color="lava" type="dots" />
              <Text size="xs" c="dimmed">
                Genie is thinking…
              </Text>
            </Group>
          )}
        </Stack>
      </Box>

      {/* Input */}
      <Group gap="xs" p="xs" wrap="nowrap" style={{ borderTop: "1px solid #e9ecef" }}>
        <Textarea
          placeholder="Ask Genie…"
          value={input}
          onChange={(e) => setInput(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              submit();
            }
          }}
          autosize
          minRows={1}
          maxRows={4}
          style={{ flex: 1 }}
          disabled={busy}
        />
        <ActionIcon
          size={36}
          color="lava"
          variant="filled"
          onClick={submit}
          disabled={busy || !input.trim()}
          aria-label="Send"
        >
          <IconSend size={18} />
        </ActionIcon>
      </Group>
    </Paper>
  );
}
