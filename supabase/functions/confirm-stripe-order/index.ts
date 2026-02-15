// Verify a Stripe Checkout Session and create the order in Supabase.
// Set STRIPE_SECRET_KEY and SUPABASE_SERVICE_ROLE_KEY in Edge Function secrets.

import "jsr:@supabase/functions-js/edge-runtime.d.ts";
import Stripe from "https://esm.sh/stripe@14.21.0?target=deno";
import { createClient } from "https://esm.sh/@supabase/supabase-js@2.45.0";

const corsHeaders = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Headers": "authorization, x-client-info, apikey, content-type",
};

function generateOrderId() {
  return "STKZ-" + Math.random().toString(36).substr(2, 9).toUpperCase();
}

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response("ok", { headers: corsHeaders });
  }

  const stripeSecret = Deno.env.get("STRIPE_SECRET_KEY");
  const supabaseUrl = Deno.env.get("SUPABASE_URL");
  const supabaseServiceKey = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

  if (!stripeSecret || !supabaseUrl || !supabaseServiceKey) {
    return new Response(
      JSON.stringify({ message: "Server configuration error" }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }

  try {
    const { session_id } = await req.json();
    if (!session_id) {
      return new Response(
        JSON.stringify({ message: "Missing session_id" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const stripe = new Stripe(stripeSecret, { apiVersion: "2024-11-20.acacia" });
    const session = await stripe.checkout.sessions.retrieve(session_id, {
      expand: ["line_items"],
    });

    if (session.payment_status !== "paid") {
      return new Response(
        JSON.stringify({ message: "Payment not completed" }),
        { status: 400, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    const meta = session.metadata || {};
    const product_title = meta.product_title || "Order";
    const quantity = parseInt(meta.quantity || "1", 10) || 1;
    const total_php = parseFloat(meta.total_php || "0") || 0;
    const userId = meta.user_id || null;
    const creditsUsed = parseFloat(meta.credits_used || "0") || 0;

    const orderId = generateOrderId();
    const supabase = createClient(supabaseUrl, supabaseServiceKey);

    const { error } = await supabase.from("orders").insert([
      {
        id: orderId,
        product_title,
        quantity,
        total: total_php,
        ref_number: "STRIPE-" + session_id,
        credits_used: creditsUsed,
        status: "Processing",
        accounts_data: [],
        refund_request: null,
        user_id: userId || null,
      },
    ]);

    if (error) {
      console.error("Supabase insert error:", error);
      return new Response(
        JSON.stringify({ message: "Failed to create order" }),
        { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
      );
    }

    return new Response(
      JSON.stringify({ order_id: orderId }),
      { headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  } catch (err) {
    console.error(err);
    const message = err instanceof Error ? err.message : "Could not confirm order";
    return new Response(
      JSON.stringify({ message }),
      { status: 500, headers: { ...corsHeaders, "Content-Type": "application/json" } }
    );
  }
});
