"use client";

import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import {
  api,
  qs,
  type AgentDetail,
  type ChannelProvider,
  type Contact,
  type Conversation,
  type Group,
  type Message,
} from "@/lib/api";
import { useRealtime } from "@/lib/realtime";
import { useMe } from "@/components/Shell";
import { useApi } from "@/components/ui";
import Chat from "@/components/inbox/Chat";
import ContactPanel from "@/components/inbox/ContactPanel";
import ConversationList, { type Filter } from "@/components/inbox/ConversationList";
import styles from "@/components/inbox/inbox.module.css";

function Inbox() {
  const me = useMe();
  const isAdmin = me?.role === "admin";
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();

  const [filter, setFilter] = useState<Filter>("open");
  const [query, setQuery] = useState("");
  const [groupId, setGroupId] = useState<number | null>(null);
  const [actAs, setActAs] = useState<number | null>(null);
  const [channel, setChannel] = useState<ChannelProvider | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selectedId, setSelectedId] = useState<number | null>(() => {
    const id = Number(params.get("id"));
    return id > 0 ? id : null;
  });
  const [extra, setExtra] = useState<Conversation | null>(null); // seleccionada que no está en la lista
  const [messages, setMessages] = useState<Message[]>([]);
  const selectedRef = useRef<number | null>(null);
  selectedRef.current = selectedId;

  const groups = useApi<Group[]>("/api/groups");
  const agents = useApi<AgentDetail[]>("/api/agents");
  const agentNames = useMemo(() => new Map((agents.data ?? []).map((a) => [a.id, a.name])), [agents.data]);
  const agentName = useCallback((id: number | null) => (id == null ? null : agentNames.get(id) ?? null), [agentNames]);

  const listPath = useMemo(() => {
    const status = filter === "mine" ? undefined : filter;
    return `/api/conversations${qs({
      status,
      mine: filter === "mine" && !actAs ? true : undefined,
      agent_id: actAs ?? undefined,
      group_id: groupId ?? undefined,
      q: query.trim() || undefined,
      channel: channel ?? undefined,
    })}`;
  }, [filter, actAs, groupId, query, channel]);

  const loadList = useCallback(async () => {
    setConversations(await api<Conversation[]>(listPath));
  }, [listPath]);

  useEffect(() => {
    const t = setTimeout(() => loadList().catch(console.error), query ? 250 : 0);
    return () => clearTimeout(t);
  }, [loadList, query]);

  // ¿La conversación pertenece a la vista actual? (para actualizaciones en tiempo real)
  const matches = useCallback(
    (c: Conversation) => {
      if (groupId && c.group?.id !== groupId) return false;
      if (channel && c.channel_provider !== channel) return false;
      const owner = actAs ?? (filter === "mine" ? me?.id : undefined);
      if (owner !== undefined && c.assigned_agent?.id !== owner) return false;
      if (query.trim()) {
        const q = query.trim().toLowerCase();
        if (!(c.contact.name ?? "").toLowerCase().includes(q) && !(c.contact.wa_id ?? "").includes(q)) return false;
      }
      switch (filter) {
        case "open":
          return c.status !== "closed";
        case "unassigned":
          return c.status === "human" && !c.assigned_agent;
        case "human":
        case "bot":
        case "closed":
          return c.status === filter;
        default:
          return true;
      }
    },
    [filter, groupId, actAs, query, channel, me?.id],
  );

  const upsertConversation = useCallback(
    (c: Conversation) => {
      setConversations((prev) => {
        const rest = prev.filter((x) => x.id !== c.id);
        if (!matches(c)) return rest;
        return [c, ...rest].sort((a, b) => b.last_message_at.localeCompare(a.last_message_at));
      });
      setExtra((e) => (e && e.id === c.id ? c : e));
    },
    [matches],
  );

  // Selección: mensajes + marcar leído + URL
  useEffect(() => {
    if (!selectedId) return;
    setMessages([]);
    api<Message[]>(`/api/conversations/${selectedId}/messages?limit=100`).then(setMessages).catch(console.error);
    api(`/api/conversations/${selectedId}/read`, { method: "POST" }).catch(() => {});
    if (params.get("id") !== String(selectedId)) router.replace(`${pathname}?id=${selectedId}`, { scroll: false });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedId]);

  const selected = conversations.find((c) => c.id === selectedId) ?? (extra?.id === selectedId ? extra : null);
  useEffect(() => {
    if (!selectedId || conversations.some((c) => c.id === selectedId) || extra?.id === selectedId) return;
    api<Conversation>(`/api/conversations/${selectedId}`).then(setExtra).catch(() => setSelectedId(null));
  }, [selectedId, conversations, extra?.id]);

  const upsertMessage = useCallback((m: Message, insert = true) => {
    if (m.conversation_id !== selectedRef.current) return;
    setMessages((prev) => {
      const i = prev.findIndex((x) => x.id === m.id);
      if (i === -1) return insert ? [...prev, m] : prev;
      const next = prev.slice();
      next[i] = m;
      return next;
    });
  }, []);

  useRealtime((event, data) => {
    if (event === "message.new") {
      const m = data as Message;
      upsertMessage(m);
      if (m.conversation_id === selectedRef.current && m.direction === "in")
        api(`/api/conversations/${m.conversation_id}/read`, { method: "POST" }).catch(() => {});
    } else if (event === "message.status") {
      upsertMessage(data as Message, false);
    } else if (event === "conversation.updated") {
      upsertConversation(data as Conversation);
    }
  });

  function onContact(contact: Contact) {
    const patch = (c: Conversation) => (c.contact.id === contact.id ? { ...c, contact } : c);
    setConversations((prev) => prev.map(patch));
    setExtra((e) => (e ? patch(e) : e));
  }

  function back() {
    setSelectedId(null);
    router.replace(pathname, { scroll: false });
  }

  return (
    <div className={styles.wrap}>
      <div className={`inbox ${selected ? "with-side" : ""}`}>
        <ConversationList
          conversations={conversations}
          selectedId={selectedId}
          onSelect={setSelectedId}
          filter={filter}
          onFilter={setFilter}
          query={query}
          onQuery={setQuery}
          groups={groups.data ?? []}
          groupId={groupId}
          onGroup={setGroupId}
          isAdmin={isAdmin}
          agents={agents.data ?? []}
          actAs={actAs}
          onActAs={setActAs}
          channel={channel}
          onChannel={setChannel}
        />
        {selected ? (
          <>
            <Chat
              conversation={selected}
              messages={messages}
              me={me}
              agentName={agentName}
              onMessage={(m) => upsertMessage(m)}
              onConversation={upsertConversation}
              onBack={back}
            />
            <ContactPanel conversation={selected} onContact={onContact} onConversation={upsertConversation} />
          </>
        ) : (
          <section className="empty">Selecciona una conversación</section>
        )}
      </div>
    </div>
  );
}

export default function ConversacionesPage() {
  return (
    <Suspense fallback={null}>
      <Inbox />
    </Suspense>
  );
}
