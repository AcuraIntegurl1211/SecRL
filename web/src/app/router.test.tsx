import { render, screen } from "@testing-library/react";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { AuthGate } from "./router";
import { LoginPage } from "../pages/LoginPage";

describe("route authentication boundary", () => {
  it("redirects an unauthenticated workspace request to login", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: { message: "required" } }), { status: 401 })));
    render(<MemoryRouter initialEntries={["/models"]}><Routes><Route element={<AuthGate />}><Route path="/models" element={<div>private</div>} /></Route><Route path="/login" element={<LoginPage />} /></Routes></MemoryRouter>);
    expect(await screen.findByRole("heading", { name: "Sign in to SecRL Lite" })).toBeInTheDocument();
  });
});
