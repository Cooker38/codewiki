package com.example.demo;

import org.springframework.stereotype.Service;

/**
 * Spring service for order processing.
 */
@Service
public class OrderService {

    private final DiscountService discountService;

    public OrderService(DiscountService discountService) {
        this.discountService = discountService;
    }

    public double processOrder(int orderId, double amount) {
        return discountService.calculateDiscount(orderId, amount);
    }
}
