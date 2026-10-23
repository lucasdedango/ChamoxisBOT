from shared.schemas import Event


class EventBus:
    def __init__(self, store):
        self.store = store

    def publish(self, event: Event):
        return self.store.event(event)
