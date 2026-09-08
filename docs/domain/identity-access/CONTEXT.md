# Identity & Access

Who can authenticate, which Workspace they belong to with what Role, and who can see or grant access to a
given resource.

## Language

**Account**:
A person's authenticatable identity (email and password), independent of any one Workspace. One Account may
hold a Membership in many Workspaces.
_Avoid_: User (a different, narrower concept — see below), the human, the person

**Workspace**:
The tenant boundary. Every resource, session, and role is scoped to exactly one Workspace.

**User**:
A Workspace-scoped anchor row that other records' owner and actor fields actually point to. Despite the name,
this is not "the human" — that's Account. One Account has one User row per Workspace it belongs to.
_Avoid_: using this to mean the human — that's Account

**WorkspaceMembership**:
The mutable record joining an Account to a Workspace: its Role, and whether it's still active. Removing
someone flips this to removed rather than deleting the row.
_Avoid_: Membership (fine as shorthand once the full term is established in context)

**Role**:
One of a fixed set (owner, admin, member, viewer) held by a WorkspaceMembership, granting a fixed permission
set. Coarser than a ResourceGrant, which is per-resource.

**Invitation**:
A time-bounded offer of a Role in a Workspace, sent to an email address — pending until accepted, rejected,
revoked, or expired.

**Session**:
A live, authenticated connection tied to one Account-in-one-Workspace pairing. Revoked independently when a
membership is removed, not merely as a side effect of the membership's own status.

**Visibility**:
A resource's default access tier: private, workspace (visible via Role alone), or shared_explicitly (visible
only via Role plus an explicit ResourceGrant).

**ResourceGrant**:
An explicit, revocable, optionally time-limited exception giving one Account a specific action on one
resource — only meaningful when that resource's Visibility is shared_explicitly.
_Avoid_: Grant (fine as shorthand once established), Permission

**OwnershipTransfer**:
A single-step, immediate reassignment of a resource's owner to a different Account.

**Delegation**:
A proposed-then-accepted handoff of an obligation from one Account to another within a Workspace, which
grants the recipient read access to whatever evidence the delegator names.

## Open question

"Resource" names two different things depending on context: a grantable, ownable database row (a
ResourceGrant's subject) and a Delegation's subject-matter target (its named evidence items). They happen to
overlap in practice, but the word doesn't distinguish them — worth a canonical split if this keeps causing
confusion in discussion.
