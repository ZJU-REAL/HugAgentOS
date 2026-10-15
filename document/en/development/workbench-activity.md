# Workbench badges, notifications and background resources

Application permissions badge, notification and background request separate capabilities.
A workspace module must also declare the corresponding grants: badge.update, notification.show and background.
Users approve each capability in host controls. SDK requests and a plugin-reported gesture never grant permission.

Opened instances call updateBadge(connection, count) or showNotification(connection, {title, body}).
Badge counts are integers from 0 to 9999, limited to five updates per instance per ten seconds.
Notification titles allow 80 characters and bodies allow 240. The host renders plain text.
Control characters and bidirectional text control characters are rejected.
Each instance may send three notifications per minute. Each user may receive ten per minute.
The host retains at most twenty notifications for five minutes. Open actions use trusted host navigation, without plugin URLs.

A badge is a controlled projection retained for at most one hour.
It may remain visible after closing the page. Reading badges never creates frames or processes.
Both sidebar layouts and More use the same state. Accessible names include unread counts and background status.
Revocation or identity, installation and revision changes clear affected state. Names and slugs never select another source.

Background execution never starts an unopened application. Users can promote only an existing application-owned resource.
This needs independent background approval. The resource service retains ownership, execution source, resource budgets and cleanup.
Each user may reserve two background resources and each application may reserve one.
Leases last at most ninety seconds. A run lasts at most fifteen minutes.
Host controls renew leases or stop actual resources. Expiration, revocation, uninstall and logout stop resources.
A resource lease does not grant background permission.

Approval is scoped to the account, exact application key, installation, revision and service origin.
An upgrade requires new approval. JSON writes retain existing authentication and CSRF controls.
Activity uses a separate workbench_activity ContentBlock. It does not alter conversations, managed business applications or navigation preferences.
