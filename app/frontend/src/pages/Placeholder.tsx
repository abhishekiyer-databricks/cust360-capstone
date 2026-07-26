import { Card, Stack, Text, Title } from "@mantine/core";

export default function Placeholder({ title, note }: { title: string; note: string }) {
  return (
    <Stack>
      <Title order={2}>{title}</Title>
      <Card withBorder radius="md" padding="xl">
        <Text c="dimmed">{note}</Text>
      </Card>
    </Stack>
  );
}
