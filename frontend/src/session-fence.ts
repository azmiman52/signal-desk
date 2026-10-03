// UI response ordering only. Authorization is always enforced by the server session.
export class SessionFence {
  private generation = 0;
  invalidate() { return ++this.generation; }
  ticket() { return this.generation; }
  accepts(ticket: number, expectedAccount: string, responseAccount: string) {
    return ticket === this.generation && expectedAccount === responseAccount;
  }
  current(ticket: number) { return ticket === this.generation; }
}
