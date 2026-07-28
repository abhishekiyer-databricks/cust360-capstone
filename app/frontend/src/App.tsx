import { Suspense, lazy } from "react";
import { Center, Loader } from "@mantine/core";
import { Navigate, Route, Routes } from "react-router-dom";

import AppLayout from "./components/AppLayout";

// Code-split routes so the initial bundle stays small (master_plan §7 React perf).
const Customers = lazy(() => import("./pages/Customers"));
const CustomerDetail = lazy(() => import("./pages/CustomerDetail"));
const Dashboard = lazy(() => import("./pages/Dashboard"));
const Reports = lazy(() => import("./pages/Reports"));

export default function App() {
  return (
    <AppLayout>
      <Suspense
        fallback={
          <Center h="60vh">
            <Loader color="lava" />
          </Center>
        }
      >
        <Routes>
          <Route path="/" element={<Navigate to="/customers" replace />} />
          <Route path="/customers" element={<Customers />} />
          <Route path="/customers/:id" element={<CustomerDetail />} />
          <Route path="/dashboard" element={<Dashboard />} />
          <Route path="/reports" element={<Reports />} />
          <Route path="*" element={<Navigate to="/customers" replace />} />
        </Routes>
      </Suspense>
    </AppLayout>
  );
}
