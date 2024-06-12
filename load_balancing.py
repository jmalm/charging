import appdaemon.plugins.hass.hassapi as hass


class LoadBalancer(hass.Hass):

    def initialize(self):
        self.main_fuse_A = int(self.args['main_fuse_A'])

        self.current_l1_entity_id = str(self.args['current_l1_entity_id'])
        self.current_l2_entity_id = str(self.args['current_l2_entity_id'])
        self.current_l3_entity_id = str(self.args['current_l3_entity_id'])
        self.current_l1_entity = self.get_entity(self.current_l1_entity_id)
        self.current_l2_entity = self.get_entity(self.current_l2_entity_id)
        self.current_l3_entity = self.get_entity(self.current_l3_entity_id)

        self.listen_state(self.balance, self.current_l1_entity_id)
        self.listen_state(self.balance, self.current_l2_entity_id)
        self.listen_state(self.balance, self.current_l3_entity_id)

    def balance(self, entity, attribute, old, new, kwargs):
        l1 = float(self.current_l1_entity.state)
        l2 = float(self.current_l2_entity.state)
        l3 = float(self.current_l3_entity.state)

        if l1 > self.main_fuse_A:
            self.log(f"L1 is too high: {l1}", level="WARNING")
        
        if l2 > self.main_fuse_A:
            self.log(f"L2 is too high: {l2}", level="WARNING")
        
        if l3 > self.main_fuse_A:
            self.log(f"L3 is too high: {l3}", level="WARNING")