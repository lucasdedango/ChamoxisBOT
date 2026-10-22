"""Market access is independent of the Plex bot and persists in its own store."""


class Access:
    def __init__(self, store, admins, configured=()):
        self.store, self.admins, self.configured = store, set(admins), set(configured)
        self.state = store.get('market:access', {'added': [], 'blocked': []})

    def administrator(self, user_id):
        return user_id in self.admins

    def allowed(self, user_id):
        return self.administrator(user_id) or (user_id not in self.state['blocked'] and
            (user_id in self.configured or user_id in self.state['added']))

    def set_user(self, actor, user_id, allow):
        if not self.administrator(actor):
            raise PermissionError('Administrateur Market requis')
        if self.administrator(user_id):
            raise ValueError('Les administrateurs configurés conservent leur accès.')
        added, blocked = set(self.state['added']), set(self.state['blocked'])
        (added if allow else blocked).add(user_id)
        (blocked if allow else added).discard(user_id)
        state = {'added': sorted(added), 'blocked': sorted(blocked)}
        self.store.set('market:access', state)
        self.state = state
