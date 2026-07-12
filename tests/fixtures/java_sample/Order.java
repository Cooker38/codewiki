package com.example.demo;

import lombok.Getter;
import lombok.Setter;
import lombok.Data;

/**
 * Represents a customer order.
 */
@Data
public class Order {

    @Getter
    private int id;

    @Getter
    @Setter
    private double amount;

    private OrderStatus status;

    public Order(int id, double amount) {
        this.id = id;
        this.amount = amount;
        this.status = OrderStatus.PENDING;
    }

    public void process() {
        this.status = OrderStatus.PROCESSING;
    }
}
