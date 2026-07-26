import { AppShell, Badge, Group, NavLink, Text, Title, ActionIcon, Tooltip } from "@mantine/core";
import {
  IconUsers,
  IconChartBar,
  IconReportAnalytics,
  IconMessageChatbot,
} from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { NavLink as RouterNavLink, useLocation, useNavigate } from "react-router-dom";

import { whoami } from "../api/client";

const NAV = [
  { label: "Customers", to: "/customers", icon: IconUsers, enabled: true },
  { label: "Dashboard", to: "/dashboard", icon: IconChartBar, enabled: false },
  { label: "Reports", to: "/reports", icon: IconReportAnalytics, enabled: false },
];

export default function AppLayout({ children }: { children: React.ReactNode }) {
  const location = useLocation();
  const navigate = useNavigate();

  // Top-bar identity. staleTime long — identity doesn't change within a session.
  const { data: me } = useQuery({
    queryKey: ["whoami"],
    queryFn: whoami,
    staleTime: 30 * 60 * 1000,
    retry: false,
  });

  return (
    <AppShell
      header={{ height: 56 }}
      navbar={{ width: 230, breakpoint: "sm" }}
      padding="md"
    >
      <AppShell.Header>
        <Group h="100%" px="md" justify="space-between">
          <Group gap="xs">
            <div
              style={{
                width: 22,
                height: 22,
                borderRadius: 6,
                background: "var(--mantine-color-lava-5)",
              }}
            />
            <Title order={4} c="navy.7">
              Customer 360
            </Title>
          </Group>
          <Group gap="sm">
            <Badge variant="light" color="navy">
              Azure Field Eng East
            </Badge>
            <Text size="sm" c="dimmed">
              {me?.email_from_header ?? me?.user_name ?? "…"}
            </Text>
          </Group>
        </Group>
      </AppShell.Header>

      <AppShell.Navbar p="sm" bg="navy.7">
        {NAV.map((item) => {
          const active = location.pathname.startsWith(item.to);
          const common = {
            label: item.label,
            leftSection: <item.icon size={18} />,
            active,
            variant: "filled" as const,
            color: "lava",
            styles: {
              root: { borderRadius: 8, color: active ? "white" : "#b6c7cf" },
              label: { fontWeight: 550 },
            },
          };
          // Two concrete branches (not a union-typed prop bag) so Mantine's polymorphic
          // NavLink generic resolves cleanly: enabled → react-router link; disabled →
          // plain button-style NavLink.
          return item.enabled ? (
            <NavLink key={item.to} component={RouterNavLink} to={item.to} {...common} />
          ) : (
            <NavLink key={item.to} disabled {...common} />
          );
        })}
      </AppShell.Navbar>

      <AppShell.Main bg="#F6F5F2">{children}</AppShell.Main>

      {/* Floating "Ask Genie" button (stub — wired in T5). */}
      <Tooltip label="Ask Genie (coming in T5)" position="left">
        <ActionIcon
          size={54}
          radius="xl"
          color="lava"
          variant="filled"
          onClick={() => navigate(location.pathname)}
          style={{
            position: "fixed",
            bottom: 24,
            right: 24,
            boxShadow: "0 4px 14px rgba(0,0,0,0.25)",
          }}
        >
          <IconMessageChatbot size={26} />
        </ActionIcon>
      </Tooltip>
    </AppShell>
  );
}
