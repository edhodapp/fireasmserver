// SPDX-License-Identifier: AGPL-3.0-or-later
//
// L2 transitions — per-event state-transition handlers.
//
// FSA-1 scope: empty placeholder. In FSA-1 the dispatcher carries
// the per-event logic inline. FSA-2 will pull the RX-consume and
// TX-submit transitions out into named, callable functions here,
// each with the contract
//
//   bool l2_rx_handle(void)    // returns true if a frame was consumed
//   bool l2_tx_handle(void)    // returns true if a TX slot was reclaimed
//
// and the dispatcher loop in dispatcher.S will iterate over them
// until both return false (multi-frame RX, task #16).
//
// Per Astier (D012), transitions in an FSA engine run to
// completion against a single event from a wait queue; they
// never block mid-execution. The MVP RX/TX paths already meet
// that shape (no blocking loops inside the consume/submit
// blocks — only barrier-protected device-memory accesses), so
// FSA-2 is mostly a refactor with a multi-frame outer loop, not
// a redesign.
